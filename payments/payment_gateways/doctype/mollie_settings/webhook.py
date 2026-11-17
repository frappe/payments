# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

import hashlib
import json
import re

import frappe
from frappe.utils import add_to_date, now_datetime

from payments.recurring import (
	RecurringPaymentValidationError,
	emit_recurring_event,
	normalize_provider_events,
)
from payments.utils import get_payment_gateway_controller

from .mollie_settings import MollieResourceNotFound, MollieSettings

_PAYMENT_ID_RE = re.compile(r"tr_[A-Za-z0-9]{1,61}\Z")
_REQUEST_DESCRIPTION = "Mollie Recurring Notification"
_JOB = "payments.payment_gateways.doctype.mollie_settings.webhook.handle_recurring_notification"
_MAX_DELIVERY_ATTEMPTS = 12
_STALE_QUEUE_MINUTES = 15
_RECOVERY_BATCH_SIZE = 100
_RECOVERY_STATUS_BATCH_SIZE = _RECOVERY_BATCH_SIZE // 2


class MollieWebhookRejected(ValueError):
	"""A webhook hint is malformed, unrouteable, or unrelated to this integration."""


def process_webhook(payment_gateway: str, token: str, payment_id: str) -> tuple[str, bool]:
	"""Fetch authoritative state and durably log one account-scoped transition.

	Returns ``(Integration Request name, should_enqueue)``. The posted body is
	never persisted or used for payment state.
	"""
	if not isinstance(payment_id, str) or not _PAYMENT_ID_RE.fullmatch(payment_id):
		raise MollieWebhookRejected("Invalid Mollie payment id")
	controller = _resolve_controller(payment_gateway)
	if not controller.webhook_token_matches(token):
		raise MollieWebhookRejected("Invalid Mollie webhook route")

	try:
		provider_event = controller.fetch_authoritative_event(payment_id)
		events = normalize_provider_events(payment_gateway, provider_event)
	except RecurringPaymentValidationError as exception:
		raise MollieWebhookRejected(str(exception)) from exception
	if not events:
		raise MollieWebhookRejected("Mollie transition produced no recurring events")

	transition_material = "\x1f".join(sorted(event["event_id"] for event in events))
	transition_hash = hashlib.sha256(f"{payment_gateway}\x1f{transition_material}".encode()).hexdigest()
	log_name = f"mollie-recurring-{transition_hash}"

	if frappe.db.exists("Integration Request", log_name):
		status, stored_data = frappe.db.get_value("Integration Request", log_name, ["status", "data"])
		attempts = _delivery_attempts(stored_data)
		if status == "Completed" or attempts >= _MAX_DELIVERY_ATTEMPTS:
			return log_name, False
		payload = _transport_payload(payment_gateway, provider_event, attempts)
		frappe.db.set_value(
			"Integration Request",
			log_name,
			{"status": "Queued", "error": None, "data": payload},
			update_modified=False,
		)
		return log_name, True

	payload = _transport_payload(payment_gateway, provider_event, 0)

	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"name": log_name,
			"integration_request_service": payment_gateway,
			"request_description": _REQUEST_DESCRIPTION,
			"request_id": f"mollie:{transition_hash}",
			"data": payload,
			"is_remote_request": 1,
			"status": "Queued",
		}
	)
	# Frappe naming clears a mapping-supplied ``name`` before calling the
	# Integration Request autoname method. Pass the deterministic transition name
	# explicitly so the database primary key provides duplicate-race protection.
	log.insert(ignore_permissions=True, ignore_if_duplicate=True, set_name=log_name)
	# A concurrent insert can win after the exists check. Its deterministic name
	# and the deterministic RQ job id below keep the handoff idempotent.
	status, stored_data = frappe.db.get_value("Integration Request", log_name, ["status", "data"])
	return log_name, status != "Completed" and _delivery_attempts(stored_data) < _MAX_DELIVERY_ATTEMPTS


@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def mollie_webhook(gateway: str = "", token: str = ""):
	"""Mollie posts only a payment id; all payment state is fetched remotely."""
	payment_id = frappe.form_dict.get("id")
	try:
		name, should_enqueue = process_webhook(gateway, token, payment_id)
	except (MollieWebhookRejected, MollieResourceNotFound) as exception:
		# Malformed/forged routes and unknown ids cannot become valid by retrying.
		frappe.db.rollback()
		frappe.log_error(
			f"Mollie webhook rejected: {exception}",
			f"Mollie payment id: {payment_id or 'unknown'}",
		)
		return

	if not should_enqueue:
		return

	# The worker reads this durable account-scoped resource bundle by name.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	_enqueue_notification(name)


def handle_recurring_notification(doctype, docname):
	log = frappe.get_doc(doctype, docname)
	if log.status == "Completed":
		return
	data = json.loads(log.data)
	attempts = _delivery_attempts(data) + 1
	if attempts > _MAX_DELIVERY_ATTEMPTS:
		return
	data["delivery_attempts"] = attempts
	frappe.db.set_value(
		doctype,
		docname,
		{"status": "Queued", "data": json.dumps(data, ensure_ascii=False, separators=(",", ":"))},
		update_modified=False,
	)
	# Persist the attempt boundary before invoking consumers. A worker crash then
	# becomes a stale Queued row that the bounded recovery scheduler can see.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
	try:
		payment_gateway = data["payment_gateway"]
		provider_event = data["provider_event"]
		for event in normalize_provider_events(payment_gateway, provider_event):
			emit_recurring_event(event)
	except Exception:
		frappe.db.rollback()
		traceback = frappe.get_traceback()
		frappe.log_error("Mollie recurring notification failed")
		failed_log = frappe.get_doc(doctype, docname)
		failed_log.handle_failure({"traceback": traceback})
		# Failure audit must survive the background worker's subsequent rollback.
		frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
		raise
	else:
		frappe.db.set_value(doctype, docname, "status", "Completed")


def retry_failed_notifications():
	"""Requeue a bounded batch of Failed and stale Queued Mollie transports."""
	stale_before = add_to_date(now_datetime(), minutes=-_STALE_QUEUE_MINUTES, as_datetime=True)
	# Reserve an independent bounded quota for each recovery class. Exhausted
	# Failed poison rows are filtered below, so they must not consume the query
	# budget needed to recover stale Queued work.
	failed = frappe.get_all(
		"Integration Request",
		filters={
			"request_description": _REQUEST_DESCRIPTION,
			"integration_request_service": ["like", "Mollie-%"],
			"status": "Failed",
		},
		pluck="name",
		limit=_RECOVERY_STATUS_BATCH_SIZE,
	)
	stale = frappe.get_all(
		"Integration Request",
		filters={
			"request_description": _REQUEST_DESCRIPTION,
			"integration_request_service": ["like", "Mollie-%"],
			"status": "Queued",
			"modified": ["<", stale_before],
		},
		pluck="name",
		limit=_RECOVERY_STATUS_BATCH_SIZE,
	)
	for name in dict.fromkeys([*failed, *stale]):
		data = frappe.db.get_value("Integration Request", name, "data")
		if _delivery_attempts(data) < _MAX_DELIVERY_ATTEMPTS:
			_enqueue_notification(name)


def _enqueue_notification(name):
	frappe.enqueue(
		method=_JOB,
		queue="short",
		doctype="Integration Request",
		docname=name,
		job_id=f"mollie-recurring-delivery:{name}",
		deduplicate=True,
	)


def _transport_payload(payment_gateway, provider_event, delivery_attempts):
	return json.dumps(
		{
			"payment_gateway": payment_gateway,
			"provider_event": provider_event,
			"delivery_attempts": delivery_attempts,
		},
		ensure_ascii=False,
		separators=(",", ":"),
	)


def _delivery_attempts(data):
	try:
		payload = json.loads(data) if isinstance(data, str) else data
		attempts = payload.get("delivery_attempts", 0)
	except (AttributeError, TypeError, ValueError):
		return _MAX_DELIVERY_ATTEMPTS
	return (
		attempts
		if type(attempts) is int and 0 <= attempts <= _MAX_DELIVERY_ATTEMPTS
		else _MAX_DELIVERY_ATTEMPTS
	)


def _resolve_controller(payment_gateway):
	if not isinstance(payment_gateway, str) or not payment_gateway.startswith("Mollie-"):
		raise MollieWebhookRejected("Invalid Mollie payment gateway route")
	if not frappe.db.exists("Payment Gateway", payment_gateway):
		raise MollieWebhookRejected("Unknown Mollie payment gateway route")
	try:
		controller = get_payment_gateway_controller(payment_gateway)
	except frappe.ValidationError as exception:
		raise MollieWebhookRejected("Invalid Mollie settings route") from exception
	if not isinstance(controller, MollieSettings):
		raise MollieWebhookRejected("Payment gateway is not a Mollie controller")
	if controller.payment_gateway != payment_gateway:
		raise MollieWebhookRejected("Mollie controller route mismatch")
	return controller
