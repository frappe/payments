# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from hmac import compare_digest
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_request_session, get_url

from payments.recurring import RecurringPaymentCapabilityError, RecurringPaymentValidationError
from payments.utils import create_payment_gateway

MOLLIE_API_BASE = "https://api.mollie.com/v2"
_PAYMENT_STATUSES = {"open", "pending", "authorized", "paid", "failed", "canceled", "expired"}
_SUBSCRIPTION_STATUSES = {"pending", "active", "canceled", "suspended", "completed"}
_MANDATE_STATUSES = {"pending", "valid", "invalid", "revoked", "expired"}
_CANCEL_RECEIPT_TTL = 60 * 60 * 24 * 30


class MollieResourceNotFound(Exception):
	"""An account-scoped Mollie resource does not exist."""


class MollieMandatePending(Exception):
	"""A paid first payment does not have an authoritative mandate yet."""


class MollieSettings(Document):
	def validate(self):
		api_key = self.api_key
		if api_key == "********":
			api_key = self.get_password("api_key", raise_exception=False)
		if not isinstance(api_key, str) or not api_key.startswith(("test_", "live_")):
			frappe.throw(_("Mollie API Key must start with test_ or live_"))

	def before_insert(self):
		if not self.webhook_token:
			self.webhook_token = frappe.generate_hash(length=40)

	def on_update(self):
		create_payment_gateway(
			self.payment_gateway,
			settings="Mollie Settings",
			controller=self.gateway_name,
		)
		if self.enabled:
			call_hook_method("payment_gateway_enabled", gateway=self.payment_gateway)

	@property
	def payment_gateway(self):
		return f"Mollie-{self.gateway_name}"

	def get_recurring_webhook_url(self):
		token = self.get_password("webhook_token", raise_exception=False)
		if not token:
			frappe.throw(_("Save Mollie Settings to generate its webhook routing token"))
		query = urlencode({"gateway": self.payment_gateway, "token": token})
		return (
			get_url("/api/method/payments.payment_gateways.doctype.mollie_settings.webhook.mollie_webhook")
			+ f"?{query}"
		)

	def begin_first_payment(self, request):
		self._require_enabled()
		self._require_account_webhook_url(request)
		customer_id = request.get("provider_customer_id")
		if customer_id:
			_require_mollie_id(customer_id, "cst_", "provider_customer_id")
			_assert_customer_ownership(self._get(f"/customers/{customer_id}"), request)
		if not customer_id:
			customer = self._post(
				"/customers",
				{
					"name": request["customer_name"],
					"email": request["customer_email"],
					**({"locale": request["customer_locale"]} if request.get("customer_locale") else {}),
					"metadata": _provider_metadata(request, include_merchant=False),
				},
				idempotency_key=_derived_idempotency_key(
					self.gateway_name, request["idempotency_key"], "customer"
				),
			)
			customer_id = _resource_id(customer, "cst_", "customer")

		payment = self._post(
			"/payments",
			{
				"amount": {"currency": request["currency"], "value": request["amount"]},
				"customerId": customer_id,
				"description": request["description"],
				"metadata": _provider_metadata(request),
				"redirectUrl": request["redirect_url"],
				"sequenceType": "first",
				"webhookUrl": request["webhook_url"],
			},
			idempotency_key=_derived_idempotency_key(
				self.gateway_name, request["idempotency_key"], "first-payment"
			),
		)
		snapshot = _payment_snapshot(payment)
		if snapshot.get("provider_customer_id") != customer_id:
			raise RecurringPaymentValidationError("Mollie payment changed provider_customer_id")
		_assert_correlated(snapshot, request)
		return snapshot

	def activate_subscription(self, request):
		self._require_enabled()
		self._require_account_webhook_url(request)
		if request.get("mandate_status") != "valid":
			raise RecurringPaymentValidationError("Mollie subscription activation requires a valid mandate")
		_require_mollie_id(request.get("provider_customer_id"), "cst_", "provider_customer_id")
		_require_mollie_id(request.get("provider_mandate_id"), "mdt_", "provider_mandate_id")
		payload = {
			"amount": {"currency": request["currency"], "value": request["amount"]},
			"description": request["description"],
			"interval": _to_mollie_interval(request["interval"]),
			"mandateId": request["provider_mandate_id"],
			"metadata": _provider_metadata(request, provider_mandate_id=request["provider_mandate_id"]),
			"startDate": request["start_date"],
			"webhookUrl": request["webhook_url"],
		}
		if request.get("payment_count") is not None:
			payload["times"] = request["payment_count"]
		subscription = self._post(
			f"/customers/{request['provider_customer_id']}/subscriptions",
			payload,
			idempotency_key=_derived_idempotency_key(
				self.gateway_name, request["idempotency_key"], "subscription"
			),
		)
		snapshot = _subscription_snapshot(subscription)
		_assert_correlated(snapshot, request)
		return snapshot

	def cancel_subscription(self, request):
		self._require_enabled()
		_require_mollie_id(request.get("provider_customer_id"), "cst_", "provider_customer_id")
		_require_mollie_id(request.get("provider_subscription_id"), "sub_", "provider_subscription_id")
		path = (
			f"/customers/{request['provider_customer_id']}"
			f"/subscriptions/{request['provider_subscription_id']}"
		)

		# The bounded Redis receipt is written only after an authoritative GET. It
		# lets the exact same operation recognize a later 404 without committing
		# the consumer's database transaction or blessing an arbitrary unknown id.
		receipt = _load_cancel_receipt(self.gateway_name, request)
		if receipt:
			before_snapshot = receipt["snapshot"]
			_assert_correlated(before_snapshot, request)
			if receipt["state"] == "canceled":
				return before_snapshot
		else:
			before_snapshot = _subscription_snapshot(self._get(path))
			_assert_correlated(before_snapshot, request)
			_store_cancel_receipt(self.gateway_name, request, "known", before_snapshot)

		if before_snapshot["status"] == "canceled":
			_store_cancel_receipt(self.gateway_name, request, "canceled", before_snapshot)
			return before_snapshot

		try:
			self._delete(
				path,
				idempotency_key=_derived_idempotency_key(
					self.gateway_name, request["idempotency_key"], "cancel-subscription"
				),
			)
		except MollieResourceNotFound:
			canceled = _canceled_snapshot(before_snapshot)
			_store_cancel_receipt(self.gateway_name, request, "canceled", canceled)
			return canceled

		try:
			after = self._get(path)
		except MollieResourceNotFound:
			canceled = _canceled_snapshot(before_snapshot)
			_store_cancel_receipt(self.gateway_name, request, "canceled", canceled)
			return canceled
		after_snapshot = _subscription_snapshot(after)
		_assert_correlated(after_snapshot, request)
		state = "canceled" if after_snapshot["status"] == "canceled" else "known"
		_store_cancel_receipt(self.gateway_name, request, state, after_snapshot)
		return after_snapshot

	def reconcile_subscription(self, request):
		self._require_enabled()
		_require_mollie_id(request.get("provider_customer_id"), "cst_", "provider_customer_id")
		_require_mollie_id(request.get("provider_subscription_id"), "sub_", "provider_subscription_id")
		path = (
			f"/customers/{request['provider_customer_id']}"
			f"/subscriptions/{request['provider_subscription_id']}"
		)
		snapshot = _subscription_snapshot(self._get(path))
		_assert_correlated(snapshot, request)
		return snapshot

	def retry_payment(self, request):
		raise RecurringPaymentCapabilityError(
			"Mollie controls subscription retries; retry_payment cannot create a replacement charge"
		)

	def normalize_provider_events(self, provider_event):
		if set(provider_event) - {"payment", "mandate", "subscription"}:
			raise RecurringPaymentValidationError("Unexpected Mollie authoritative event fields")
		payment = provider_event.get("payment")
		if not isinstance(payment, Mapping):
			raise RecurringPaymentValidationError("Mollie authoritative event requires payment")
		payment_snapshot = _payment_snapshot(payment)
		sequence_type = payment.get("sequenceType")
		if sequence_type not in {"first", "recurring"}:
			raise RecurringPaymentValidationError("Mollie payment is not a recurring-contract payment")

		mandate = provider_event.get("mandate")
		mandate_snapshot = None
		if mandate is not None:
			mandate_snapshot = _mandate_snapshot(mandate, _references(payment))
			# A paid first payment can still name a pending mandate while another
			# mandate on the same customer is already valid. Correlate ownership,
			# but preserve both authoritative resource IDs independently.
			_assert_shared(
				payment_snapshot,
				mandate_snapshot,
				fields=("merchant_reference", "customer_reference", "provider_customer_id"),
			)

		subscription = provider_event.get("subscription")
		subscription_snapshot = None
		if subscription is not None:
			subscription_snapshot = _subscription_snapshot(subscription)
			_assert_shared(payment_snapshot, subscription_snapshot)

		if sequence_type == "first" and subscription_snapshot is not None:
			raise RecurringPaymentValidationError("First Mollie payment cannot carry a subscription")
		if sequence_type == "recurring" and subscription_snapshot is None:
			raise RecurringPaymentValidationError("Recurring Mollie payment requires its subscription")

		extra = {}
		payment_transition = [payment_snapshot["provider_payment_id"], payment_snapshot["status"]]
		for field in ("refunded_amount", "charged_back_amount"):
			if field in payment_snapshot:
				payment_transition.extend([field, payment_snapshot[field]])
		# Mandates always travel in their own resource-local event. In particular,
		# do not attach a fallback valid mandate to a payment that authoritatively
		# names a different pending mandate.
		if subscription_snapshot:
			extra["subscription"] = subscription_snapshot

		payment_event_type = (
			"first_payment.updated" if sequence_type == "first" else "recurring_payment.updated"
		)
		events = [
			_event(
				payment_event_type,
				payment_snapshot,
				payment_transition,
				payment=payment_snapshot,
				**extra,
			)
		]
		if mandate_snapshot:
			events.append(
				_event(
					"mandate.updated",
					mandate_snapshot,
					[
						mandate_snapshot["provider_mandate_id"],
						mandate_snapshot["status"],
					],
					mandate=mandate_snapshot,
				)
			)
		if subscription_snapshot:
			events.append(
				_event(
					"subscription.updated",
					subscription_snapshot,
					[
						subscription_snapshot["provider_subscription_id"],
						subscription_snapshot["status"],
					],
					subscription=subscription_snapshot,
				)
			)
		return events

	def fetch_authoritative_event(self, payment_id):
		self._require_enabled()
		_require_mollie_id(payment_id, "tr_", "provider_payment_id")
		remote_payment = self._get(f"/payments/{payment_id}")
		if _resource_id(remote_payment, "tr_", "payment") != payment_id:
			raise RecurringPaymentValidationError("Mollie returned a different payment resource")
		payment = _project_payment(remote_payment)
		sequence_type = payment.get("sequenceType")
		bundle = {"payment": payment}
		if sequence_type == "first" and payment.get("status") == "paid":
			customer_id = payment.get("customerId")
			if not _valid_id(customer_id, "cst_"):
				raise RecurringPaymentValidationError("Paid first payment has no Mollie customer")
			try:
				mandates = self._get(f"/customers/{customer_id}/mandates")
			except MollieResourceNotFound as exception:
				raise MollieMandatePending(
					"Paid first payment mandate collection is not authoritative yet"
				) from exception
			items = mandates.get("_embedded", {}).get("mandates", [])
			selected = _select_mandate(items, payment.get("mandateId"))
			if not selected:
				raise MollieMandatePending(
					"Paid first payment does not have an authoritative valid mandate yet"
				)
			bundle["mandate"] = _project_mandate(selected)
		elif sequence_type == "recurring":
			customer_id = payment.get("customerId")
			subscription_id = payment.get("subscriptionId")
			if not _valid_id(customer_id, "cst_") or not _valid_id(subscription_id, "sub_"):
				raise RecurringPaymentValidationError(
					"Recurring Mollie payment has no customer-scoped subscription"
				)
			bundle["subscription"] = _project_subscription(
				self._get(f"/customers/{customer_id}/subscriptions/{subscription_id}")
			)
		elif sequence_type != "first":
			raise RecurringPaymentValidationError("Mollie payment is not first or recurring")
		return bundle

	def webhook_token_matches(self, token):
		expected = self.get_password("webhook_token", raise_exception=False) or ""
		return bool(
			isinstance(expected, str)
			and isinstance(token, str)
			and expected
			and token
			and compare_digest(expected, token)
		)

	def _require_enabled(self):
		if not self.enabled:
			frappe.throw(_("Mollie payment gateway is disabled"))
		if not self.get_password("api_key", raise_exception=False):
			frappe.throw(_("Mollie API Key is not configured"))

	def _require_account_webhook_url(self, request):
		expected = self.get_recurring_webhook_url()
		if request.get("webhook_url") != expected:
			raise RecurringPaymentValidationError(
				"Mollie webhook_url does not match the selected payment gateway account"
			)

	def _headers(self, idempotency_key=None):
		headers = {
			"Authorization": f"Bearer {self.get_password('api_key', raise_exception=False)}",
			"Accept": "application/json",
			"Content-Type": "application/json",
		}
		if idempotency_key:
			headers["Idempotency-Key"] = idempotency_key
		return headers

	def _get(self, path):
		return self._request("GET", path)

	def _post(self, path, payload, *, idempotency_key):
		return self._request("POST", path, payload=payload, idempotency_key=idempotency_key)

	def _delete(self, path, *, idempotency_key):
		return self._request("DELETE", path, idempotency_key=idempotency_key)

	def _request(self, method, path, *, payload=None, idempotency_key=None):
		if method not in {"GET", "POST", "DELETE"}:
			raise ValueError(f"Unsupported Mollie HTTP method {method}")
		url = f"{MOLLIE_API_BASE}{path}"
		try:
			response = get_request_session().request(
				method,
				url,
				headers=self._headers(idempotency_key),
				json=payload if method == "POST" else None,
				timeout=30,
			)
			response.raise_for_status()
		except Exception as exception:
			if _exception_status_code(exception) == 404:
				raise MollieResourceNotFound(path) from exception
			raise
		if method == "DELETE":
			return None
		try:
			result = response.json()
		except (TypeError, ValueError) as exception:
			raise RecurringPaymentValidationError("Mollie API returned invalid JSON") from exception
		if not isinstance(result, Mapping):
			raise RecurringPaymentValidationError("Mollie API returned a non-object response")
		return result


def _project_payment(payment):
	fields = (
		"id",
		"status",
		"amount",
		"amountRefunded",
		"amountChargedBack",
		"customerId",
		"mandateId",
		"subscriptionId",
		"sequenceType",
		"createdAt",
		"authorizedAt",
		"paidAt",
		"failedAt",
		"canceledAt",
		"expiredAt",
	)
	projected = {field: payment[field] for field in fields if payment.get(field) is not None}
	projected["metadata"] = _project_metadata(payment.get("metadata"))
	return projected


def _project_subscription(subscription):
	fields = (
		"id",
		"status",
		"amount",
		"customerId",
		"mandateId",
		"interval",
		"startDate",
		"description",
		"times",
		"createdAt",
		"canceledAt",
	)
	projected = {field: subscription[field] for field in fields if subscription.get(field) is not None}
	projected["metadata"] = _project_metadata(subscription.get("metadata"))
	return projected


def _project_mandate(mandate):
	return {
		field: mandate[field]
		for field in ("id", "status", "customerId", "createdAt")
		if mandate.get(field) is not None
	}


def _project_metadata(metadata):
	if not isinstance(metadata, Mapping):
		raise RecurringPaymentValidationError("Mollie resource has no recurring metadata")
	return {
		field: metadata[field]
		for field in (
			"payments_contract_version",
			"merchant_reference",
			"customer_reference",
			"provider_mandate_id",
		)
		if field in metadata
	}


def _provider_metadata(request, *, include_merchant=True, provider_mandate_id=None):
	metadata = {
		"payments_contract_version": 1,
		"customer_reference": request["customer_reference"],
	}
	if include_merchant:
		metadata["merchant_reference"] = request["merchant_reference"]
	if provider_mandate_id:
		metadata["provider_mandate_id"] = provider_mandate_id
	return metadata


def _assert_customer_ownership(customer, request):
	customer_id = _resource_id(customer, "cst_", "customer")
	if customer_id != request["provider_customer_id"]:
		raise RecurringPaymentValidationError("Mollie returned a different customer resource")
	metadata = customer.get("metadata")
	if not isinstance(metadata, Mapping):
		raise RecurringPaymentValidationError("Mollie customer has no recurring metadata")
	if metadata.get("payments_contract_version") != 1:
		raise RecurringPaymentValidationError("Mollie customer has an unsupported contract version")
	if metadata.get("customer_reference") != request["customer_reference"]:
		raise RecurringPaymentValidationError(
			"Mollie customer does not belong to the supplied customer_reference"
		)


def _cancel_receipt_identity(account, request):
	correlation = {
		"account": account,
		"idempotency_key": request["idempotency_key"],
		"merchant_reference": request["merchant_reference"],
		"customer_reference": request["customer_reference"],
		"provider_customer_id": request["provider_customer_id"],
		"provider_subscription_id": request["provider_subscription_id"],
	}
	encoded = json.dumps(correlation, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
	return correlation, hashlib.sha256(encoded.encode()).hexdigest()


def _cancel_receipt_key(account, request):
	_, digest = _cancel_receipt_identity(account, request)
	return f"payments:mollie:cancel:{digest}"


def _load_cancel_receipt(account, request):
	correlation, digest = _cancel_receipt_identity(account, request)
	cached = frappe.cache().get_value(_cancel_receipt_key(account, request))
	if not cached:
		return None
	try:
		receipt = json.loads(cached) if isinstance(cached, str) else cached
	except (TypeError, ValueError):
		return None
	if not isinstance(receipt, Mapping):
		return None
	if receipt.get("digest") != digest or receipt.get("correlation") != correlation:
		return None
	if receipt.get("state") not in {"known", "canceled"} or not isinstance(receipt.get("snapshot"), Mapping):
		return None
	return receipt


def _store_cancel_receipt(account, request, state, snapshot):
	correlation, digest = _cancel_receipt_identity(account, request)
	payload = json.dumps(
		{
			"digest": digest,
			"correlation": correlation,
			"state": state,
			"snapshot": dict(snapshot),
		},
		ensure_ascii=False,
		separators=(",", ":"),
	)
	frappe.cache().set_value(
		_cancel_receipt_key(account, request), payload, expires_in_sec=_CANCEL_RECEIPT_TTL
	)


def _canceled_snapshot(snapshot):
	canceled = {**snapshot, "status": "canceled"}
	# A 404 proves absence after a known cancellation attempt, but does not give
	# an authoritative provider transition timestamp.
	canceled.pop("occurred_at", None)
	canceled.pop("canceled_at", None)
	return canceled


def _references(resource):
	metadata = resource.get("metadata")
	if not isinstance(metadata, Mapping):
		raise RecurringPaymentValidationError("Mollie resource has no recurring metadata")
	if metadata.get("payments_contract_version") != 1:
		raise RecurringPaymentValidationError("Mollie resource has an unsupported contract version")
	merchant_reference = metadata.get("merchant_reference")
	customer_reference = metadata.get("customer_reference")
	if not isinstance(merchant_reference, str) or not merchant_reference:
		raise RecurringPaymentValidationError("Mollie resource has no merchant_reference")
	if not isinstance(customer_reference, str) or not customer_reference:
		raise RecurringPaymentValidationError("Mollie resource has no customer_reference")
	return {
		"merchant_reference": merchant_reference,
		"customer_reference": customer_reference,
	}


def _payment_snapshot(payment):
	payment_id = _resource_id(payment, "tr_", "payment")
	status = payment.get("status")
	if status not in _PAYMENT_STATUSES:
		raise RecurringPaymentValidationError(f"Unsupported Mollie payment status {status!r}")
	amount = _amount(payment.get("amount"), "payment amount")
	refunded_amount = _reversal_amount(payment.get("amountRefunded"), amount, "amountRefunded")
	charged_back_amount = _reversal_amount(payment.get("amountChargedBack"), amount, "amountChargedBack")
	if Decimal(refunded_amount or "0") + Decimal(charged_back_amount or "0") > Decimal(amount["value"]):
		raise RecurringPaymentValidationError(
			"Mollie refunded and charged-back amounts exceed the original payment"
		)
	if charged_back_amount:
		status = (
			"charged_back"
			if Decimal(charged_back_amount) == Decimal(amount["value"])
			else "partially_charged_back"
		)
	elif refunded_amount:
		status = "refunded" if Decimal(refunded_amount) == Decimal(amount["value"]) else "partially_refunded"
	references = _references(payment)
	snapshot = {
		"provider_payment_id": payment_id,
		"status": status,
		"amount": amount["value"],
		"currency": amount["currency"],
		**references,
	}
	if refunded_amount:
		snapshot["refunded_amount"] = refunded_amount
	if charged_back_amount:
		snapshot["charged_back_amount"] = charged_back_amount
	for mollie_field, contract_field, prefix in (
		("customerId", "provider_customer_id", "cst_"),
		("mandateId", "provider_mandate_id", "mdt_"),
		("subscriptionId", "provider_subscription_id", "sub_"),
	):
		value = payment.get(mollie_field)
		if value is not None:
			if not _valid_id(value, prefix):
				raise RecurringPaymentValidationError(f"Invalid Mollie {mollie_field}")
			snapshot[contract_field] = value
	checkout_url = payment.get("_links", {}).get("checkout", {}).get("href")
	if checkout_url:
		snapshot["checkout_url"] = checkout_url
	occurred_at = _payment_timestamp(payment, status)
	if occurred_at:
		snapshot["occurred_at"] = occurred_at
	return snapshot


def _subscription_snapshot(subscription):
	subscription_id = _resource_id(subscription, "sub_", "subscription")
	status = subscription.get("status")
	if status not in _SUBSCRIPTION_STATUSES:
		raise RecurringPaymentValidationError(f"Unsupported Mollie subscription status {status!r}")
	references = _references(subscription)
	amount = _amount(subscription.get("amount"), "subscription amount")
	customer_id = subscription.get("customerId")
	if not _valid_id(customer_id, "cst_"):
		raise RecurringPaymentValidationError("Mollie subscription has no customerId")
	metadata = subscription.get("metadata") or {}
	mandate_id = subscription.get("mandateId") or metadata.get("provider_mandate_id")
	if not _valid_id(mandate_id, "mdt_"):
		raise RecurringPaymentValidationError("Mollie subscription has no mandateId")
	start_date = _iso_date(subscription.get("startDate"), "subscription startDate")
	description = subscription.get("description")
	if not isinstance(description, str) or not description or description != description.strip():
		raise RecurringPaymentValidationError("Mollie subscription description is malformed")
	snapshot = {
		"provider_subscription_id": subscription_id,
		"status": status,
		**references,
		"provider_customer_id": customer_id,
		"provider_mandate_id": mandate_id,
		"amount": amount["value"],
		"currency": amount["currency"],
		"interval": _from_mollie_interval(subscription.get("interval")),
		"start_date": start_date,
		"description": description,
	}
	if subscription.get("times") is not None:
		snapshot["payment_count"] = subscription["times"]
	if subscription.get("canceledAt"):
		snapshot["canceled_at"] = subscription["canceledAt"]
	if status == "canceled" and subscription.get("canceledAt"):
		snapshot["occurred_at"] = subscription["canceledAt"]
	elif status in {"pending", "active"} and subscription.get("createdAt"):
		snapshot["occurred_at"] = subscription["createdAt"]
	return snapshot


def _mandate_snapshot(mandate, references):
	mandate_id = _resource_id(mandate, "mdt_", "mandate")
	status = mandate.get("status")
	if status not in _MANDATE_STATUSES:
		raise RecurringPaymentValidationError(f"Unsupported Mollie mandate status {status!r}")
	customer_id = mandate.get("customerId")
	if not _valid_id(customer_id, "cst_"):
		raise RecurringPaymentValidationError("Mollie mandate has no customerId")
	snapshot = {
		"provider_mandate_id": mandate_id,
		"status": status,
		**references,
		"provider_customer_id": customer_id,
	}
	# Mollie exposes mandate creation time but no authoritative status-transition
	# timestamp. Do not label createdAt as the time of a later valid/revoked state.
	return snapshot


def _event(event_type, snapshot, transition_parts, **snapshots):
	resource_id = next(
		value
		for field, value in snapshot.items()
		if field in {"provider_payment_id", "provider_mandate_id", "provider_subscription_id"}
	)
	digest = hashlib.sha256("\x1f".join(transition_parts).encode()).hexdigest()[:24]
	event = {
		"event_id": f"mollie:{resource_id}:{digest}",
		"event_type": event_type,
		"merchant_reference": snapshot["merchant_reference"],
		"customer_reference": snapshot["customer_reference"],
		**snapshots,
	}
	if snapshot.get("occurred_at"):
		event["provider_created_at"] = snapshot["occurred_at"]
	return event


def _select_mandate(mandates, payment_mandate_id=None):
	if not isinstance(mandates, list):
		raise RecurringPaymentValidationError("Mollie mandates response is malformed")
	if payment_mandate_id:
		for mandate in mandates:
			if mandate.get("id") == payment_mandate_id and mandate.get("status") == "valid":
				return mandate
	for mandate in mandates:
		if mandate.get("status") == "valid":
			return mandate
	return None


def _iso_date(value, label):
	if not isinstance(value, str):
		raise RecurringPaymentValidationError(f"Mollie {label} is malformed")
	try:
		parsed = date.fromisoformat(value)
	except ValueError:
		raise RecurringPaymentValidationError(f"Mollie {label} is malformed")
	if parsed.isoformat() != value:
		raise RecurringPaymentValidationError(f"Mollie {label} is malformed")
	return value


def _to_mollie_interval(interval):
	unit = {"D": "day", "W": "week", "M": "month", "Y": "year"}.get(interval[-1:])
	try:
		count = int(interval[1:-1])
	except (TypeError, ValueError):
		count = 0
	if not unit or count <= 0 or interval != f"P{count}{interval[-1]}":
		raise RecurringPaymentValidationError("Unsupported recurring interval for Mollie")
	return f"{count} {unit}{'' if count == 1 else 's'}"


def _from_mollie_interval(interval):
	if not isinstance(interval, str):
		raise RecurringPaymentValidationError("Mollie subscription interval is malformed")
	parts = interval.split(" ")
	if len(parts) != 2:
		raise RecurringPaymentValidationError("Mollie subscription interval is malformed")
	try:
		count = int(parts[0])
	except ValueError:
		count = 0
	unit = parts[1]
	units = {
		"day": "D",
		"days": "D",
		"week": "W",
		"weeks": "W",
		"month": "M",
		"months": "M",
		"year": "Y",
		"years": "Y",
	}
	if count <= 0 or unit not in units or (count == 1) != (not unit.endswith("s")):
		raise RecurringPaymentValidationError("Mollie subscription interval is malformed")
	return f"P{count}{units[unit]}"


def _amount(value, label):
	if not isinstance(value, Mapping):
		raise RecurringPaymentValidationError(f"Mollie {label} is malformed")
	amount = value.get("value")
	currency = value.get("currency")
	if not isinstance(amount, str) or not isinstance(currency, str):
		raise RecurringPaymentValidationError(f"Mollie {label} is malformed")
	try:
		decimal = Decimal(amount)
	except (InvalidOperation, ValueError):
		raise RecurringPaymentValidationError(f"Mollie {label} is malformed")
	if not decimal.is_finite() or decimal < 0:
		raise RecurringPaymentValidationError(f"Mollie {label} is malformed")
	return {"value": amount, "currency": currency}


def _reversal_amount(value, original, label):
	if value is None:
		return None
	reversal = _amount(value, f"payment {label}")
	if reversal["currency"] != original["currency"]:
		raise RecurringPaymentValidationError(f"Mollie payment {label} currency does not match")
	try:
		reversal_value = Decimal(reversal["value"])
		original_value = Decimal(original["value"])
	except InvalidOperation:
		raise RecurringPaymentValidationError(f"Mollie payment {label} is malformed")
	if not reversal_value.is_finite() or reversal_value < 0 or reversal_value > original_value:
		raise RecurringPaymentValidationError(f"Mollie payment {label} is out of range")
	return reversal["value"] if reversal_value else None


def _payment_timestamp(payment, normalized_status):
	# Mollie payment resources do not expose a trustworthy refund/chargeback
	# transition timestamp, so reversal events deliberately omit occurred_at.
	if normalized_status in {
		"partially_refunded",
		"refunded",
		"partially_charged_back",
		"charged_back",
	}:
		return None
	field = {
		"authorized": "authorizedAt",
		"paid": "paidAt",
		"failed": "failedAt",
		"canceled": "canceledAt",
		"expired": "expiredAt",
	}.get(normalized_status)
	return (payment.get(field) if field else None) or (
		payment.get("createdAt") if normalized_status in {"open", "pending"} else None
	)


def _require_mollie_id(value, prefix, field):
	if not _valid_id(value, prefix):
		raise RecurringPaymentValidationError(f"Invalid Mollie {field}")
	return value


def _resource_id(resource, prefix, label):
	if not isinstance(resource, Mapping):
		raise RecurringPaymentValidationError(f"Mollie {label} resource is malformed")
	resource_id = resource.get("id")
	if not _valid_id(resource_id, prefix):
		raise RecurringPaymentValidationError(f"Invalid Mollie {label} id")
	return resource_id


def _valid_id(value, prefix):
	return bool(
		isinstance(value, str)
		and value.startswith(prefix)
		and len(prefix) < len(value) <= 64
		and value[len(prefix) :].isalnum()
		and value[len(prefix) :].isascii()
	)


def _assert_correlated(snapshot, request):
	for field in (
		"merchant_reference",
		"customer_reference",
		"provider_customer_id",
		"provider_mandate_id",
		"provider_subscription_id",
		"amount",
		"currency",
		"interval",
		"start_date",
		"description",
		"payment_count",
	):
		if field in request and field in snapshot and snapshot[field] != request[field]:
			raise RecurringPaymentValidationError(f"Mollie resource changed correlated field {field}")


def _assert_shared(
	first,
	second,
	*,
	fields=(
		"merchant_reference",
		"customer_reference",
		"provider_customer_id",
		"provider_mandate_id",
		"provider_subscription_id",
	),
):
	for field in fields:
		if field in first and field in second and first[field] != second[field]:
			raise RecurringPaymentValidationError(f"Mollie resources disagree on {field}")


def _derived_idempotency_key(account, source_key, operation):
	return hashlib.sha256(f"mollie\x1f{account}\x1f{operation}\x1f{source_key}".encode()).hexdigest()


def _exception_status_code(exception):
	response = getattr(exception, "response", None)
	return (
		getattr(exception, "http_status_code", None)
		or getattr(exception, "status_code", None)
		or getattr(response, "status_code", None)
	)
