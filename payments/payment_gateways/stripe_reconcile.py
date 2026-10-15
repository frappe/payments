# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Maps verified Stripe webhook events onto ERPNext records; handlers are idempotent.

import frappe

from payments.payment_gateways.stripe_utils import get_stripe_client


def _status_label(result):
	"""Log label from a finaliser result. Async (bank-debit) sessions settle
	later, so surface Pending instead of masking it as Processed."""
	status = (result or {}).get("status")
	if status == "Failed":
		return "Failed"
	if status == "Pending":
		return "Pending"
	return "Processed"


def route_event(event, settings):
	handler = _HANDLERS.get(event["type"])
	if not handler:
		return {"status_label": "Ignored"}
	return handler(event, settings)


def reconcile_checkout_session(event, settings):
	"""checkout.session.completed — Hosted Checkout one-off / first payment."""
	session = event["data"]["object"]
	result = settings.finalize_checkout_session(session["id"]) or {}
	meta = dict(session.get("metadata") or {})
	return {
		"status_label": _status_label(result),
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def reconcile_one_off(event, settings):
	"""payment_intent.succeeded — backstop for one-off payments.

	PaymentIntents that belong to a Stripe invoice (subscriptions) are handled
	via invoice.paid, so they are skipped here.
	"""
	intent = event["data"]["object"]
	if intent.get("invoice"):
		return {"status_label": "Ignored"}

	meta = dict(intent.get("metadata") or {})
	if not meta.get("reference_docname"):
		return {"status_label": "Ignored"}

	result = settings.finalize_payment_intent(intent) or {}
	return {
		"status_label": _status_label(result),
		"reference_doctype": meta.get("reference_doctype"),
		"reference_name": meta.get("reference_docname"),
	}


def handle_setup_intent_succeeded(event, settings):
	"""setup_intent.succeeded — make the saved card the customer's default."""
	si = event["data"]["object"]
	customer = si.get("customer")
	payment_method = si.get("payment_method")
	if customer and payment_method:
		client = get_stripe_client(settings)
		client.customers.update(customer, {"invoice_settings": {"default_payment_method": payment_method}})
	return {"status_label": "Processed"}


_HANDLERS = {
	"checkout.session.completed": reconcile_checkout_session,
	"payment_intent.succeeded": reconcile_one_off,
	"setup_intent.succeeded": handle_setup_intent_succeeded,
}
