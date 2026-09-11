# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt
"""Whitelisted Stripe endpoints shared across apps.

The webshop checkout and the hosted ``stripe_checkout`` page (payment links) both
create and finalise their card payments here. The customer portal keeps its own
two endpoints because it has to prove document ownership, but it reuses the
helpers below and the ``Stripe Settings`` methods, so only one implementation
speaks to Stripe. What a payment *means* to the documents behind it stays with
the app, in its ``Payment Request.on_payment_authorized`` hook.

These endpoints are guest-accessible because guest checkout is supported and
payment links are sent to customers who may not be logged in. That is safe: a
PaymentIntent is only ``succeeded`` once Stripe has captured the money, and every
status change here is decided from the intent re-read from Stripe — never from
what the caller told us.
"""

import json
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import flt

from payments.payment_gateways.stripe_integration import to_stripe_amount
from payments.utils import get_payment_gateway_controller

# Payment Request statuses that can still be paid.
PAYABLE_STATUSES = ("Requested", "Initiated", "Partially Paid")

# ``Payment Request.custom_intent_status`` for a card that is held but not charged.
# The single definition of a *live* hold: a released or lapsed one must never look
# live again, or the order it is attached to can never be paid for.
AUTHORIZATION_STATUS = "requires_capture"


def get_controller(payment_request=None):
	"""Resolve the Stripe Settings record a payment belongs to.

	With a payment request we can be exact. Without one — the webhook case, where
	only the PaymentIntent id is known — the site's default gateway account is
	used, falling back to the only Stripe account if there is exactly one.
	"""
	if payment_request:
		if not payment_request.payment_gateway:
			frappe.throw(_("Payment Request {0} has no payment gateway.").format(payment_request.name))

		controller = get_payment_gateway_controller(payment_request.payment_gateway)
		if controller.doctype != "Stripe Settings":
			frappe.throw(_("Payment Request {0} is not payable by card.").format(payment_request.name))

		return controller

	payment_gateway = frappe.db.get_value("Payment Gateway Account", {"is_default": 1}, "payment_gateway")
	if payment_gateway:
		controller = get_payment_gateway_controller(payment_gateway)
		if controller.doctype == "Stripe Settings":
			return controller

	accounts = frappe.get_all("Stripe Settings", pluck="name", limit_page_length=2)
	if len(accounts) == 1:
		return frappe.get_doc("Stripe Settings", accounts[0])

	frappe.throw(
		_("Cannot tell which Stripe account this payment belongs to. Set a default Payment Gateway Account."),
		title=_("Payment gateway not configured"),
	)


def amount_of(payment_request):
	"""Amount still payable on a payment request."""
	return flt(payment_request.outstanding_amount, 2) or flt(payment_request.grand_total, 2)


def payment_metadata(payment_request, **extra):
	"""Metadata stamped on every intent, so a payment can be traced back to it."""
	metadata = {
		"payment_request": payment_request.name,
		"reference_doctype": payment_request.reference_doctype,
		"reference_name": payment_request.reference_name,
		"customer": payment_request.party or "",
	}
	metadata.update(extra)
	return metadata


def log_intent(intent, source):
	"""Mirror a PaymentIntent into Integration Request, as the other gateways do.

	Observability only — it runs after finalisation, so the commit it performs is
	harmless, and a failure here must never affect the payment itself.
	"""
	try:
		request_log = create_request_log(
			frappe.as_json(
				{
					"payment_intent": intent.get("id"),
					"amount": intent.get("amount"),
					"currency": intent.get("currency"),
					"source": source,
				}
			),
			service_name="Stripe",
			reference_doctype="Payment Request",
			reference_docname=(intent.get("metadata") or {}).get("payment_request"),
		)
		request_log.db_set("status", "Completed", update_modified=False)
	except Exception:
		frappe.log_error(
			title="Stripe: could not log the PaymentIntent",
			message=frappe.get_traceback(),
		)


def get_open_payment_request(reference_doctype, reference_docname):
	"""The open Payment Request being paid — or the given one itself."""
	if reference_doctype == "Payment Request":
		payment_request = frappe.get_doc("Payment Request", reference_docname)
		if payment_request.docstatus != 1 or payment_request.status not in PAYABLE_STATUSES:
			frappe.throw(_("Payment Request {0} is not open for payment.").format(payment_request.name))

		return payment_request

	name = frappe.db.get_value(
		"Payment Request",
		{
			"reference_doctype": reference_doctype,
			"reference_name": reference_docname,
			"docstatus": 1,
			"status": ["in", PAYABLE_STATUSES],
		},
		"name",
	)
	if not name:
		frappe.throw(
			_("No open payment request found for {0} {1}.").format(reference_doctype, reference_docname)
		)

	return frappe.get_doc("Payment Request", name)


def resolve_payment_request_from_intent(intent):
	"""The Payment Request a PaymentIntent belongs to, or None.

	Every intent carries the Payment Request in its metadata (see
	``payment_metadata``). That link is what makes a webhook usable at all: Stripe
	sends us an intent, not a document.
	"""
	payment_request = (intent.get("metadata") or {}).get("payment_request")
	if not payment_request or not frappe.db.exists("Payment Request", payment_request):
		return None

	return frappe.get_doc("Payment Request", payment_request)


def _deferred_capture_fields_available():
	"""Whether the deferred-capture fields exist on Payment Request.

	They are declared by the webshop app, because deferred capture is a *checkout*
	behaviour - the cart authorises and the customer approves later. This app has to
	keep working on a site without webshop, so everything below degrades to "there is
	no hold" rather than querying a column that is not there.

	Checked through the cached doctype meta rather than ``frappe.db.has_column``,
	because ``has_active_authorization`` is called once per document by the portal's
	list views.
	"""
	return frappe.get_meta("Payment Request").has_field("custom_intent_status")


def _authorization_filters(reference_doctype, reference_docname):
	"""What makes a draft Payment Request a *live* card hold for a document.

	A cart order whose card was only held is submitted straight away, while its
	Payment Request deliberately stays a draft until the money actually moves - so a
	draft ``requires_capture`` request *is* the hold. Kept in one place because three
	things depend on agreeing about it: the portal's "already authorised, don't charge
	again" guard, the lead-time pages, and housekeeping.
	"""
	return {
		"reference_doctype": reference_doctype,
		"reference_name": reference_docname,
		"docstatus": 0,
		"custom_intent_status": AUTHORIZATION_STATUS,
	}


def has_active_authorization(reference_doctype, reference_docname):
	"""Whether a card hold is alive for a document. Cheap enough for list views."""
	if not reference_doctype or not reference_docname:
		return False
	if not _deferred_capture_fields_available():
		return False

	return bool(
		frappe.db.exists("Payment Request", _authorization_filters(reference_doctype, reference_docname))
	)


def get_active_authorization(reference_doctype, reference_docname):
	"""The draft Payment Request holding a live card authorisation for a document.

	Returns ``None`` when there is no hold, or when the one there was has been voided,
	captured or has lapsed.

	This is the read side of the deferred-capture flow: the customer portal uses it to
	decide what an order is waiting on, and to start offering payment again for an
	order whose hold has gone.
	"""
	if not _deferred_capture_fields_available():
		return None

	name = frappe.db.get_value(
		"Payment Request", _authorization_filters(reference_doctype, reference_docname), "name"
	)
	return frappe.get_doc("Payment Request", name) if name else None


def mark_authorization_released(intent, status="canceled"):
	"""Stop a document behaving as if a released card hold were still live.

	The hold is released either by us (the customer declined) or by Stripe (the
	authorisation window lapsed). Either way the order has to stop looking held,
	because a live hold is what refuses further payment - a stale one would leave the
	order permanently unpayable.

	Idempotent, and it never touches a request that was actually paid. The order's
	own status is only filled in when it is still open, so a ``Declined`` or
	``Approved`` set by our own endpoint is not overwritten by a racing webhook.
	"""
	if not _deferred_capture_fields_available():
		return None

	payment_request = resolve_payment_request_from_intent(intent)
	if not payment_request:
		return None

	if payment_request.docstatus == 1 or payment_request.status == "Paid":
		return None

	payment_request.db_set("custom_intent_status", status, update_modified=False)

	if payment_request.reference_doctype != "Sales Order":
		return payment_request.name

	if not frappe.db.exists("Sales Order", payment_request.reference_name):
		return payment_request.name

	# The Sales Order field is webshop's too, and only webshop orders enter the
	# lead-time flow - without it there is simply no order status to move.
	if not frappe.get_meta("Sales Order").has_field("custom_lead_time_status"):
		return payment_request.name

	sales_order = frappe.get_doc("Sales Order", payment_request.reference_name)
	if sales_order.get("custom_lead_time_status") in ("Pending Review", "Awaiting Customer Approval"):
		sales_order.db_set("custom_lead_time_status", "Expired", update_modified=False)

	return payment_request.name


def fallback_redirect(payment_request):
	"""Where to send a customer when the app hook had no opinion.

	Keeps the convention the legacy gateway flow used, so the existing
	``payment-success`` page keeps working.
	"""
	return "payment-success?" + urlencode(
		{"doctype": payment_request.reference_doctype, "docname": payment_request.reference_name}
	)


@frappe.whitelist(allow_guest=True)
def create_payment_intent_for_reference(reference_doctype, reference_docname):
	"""Start a card payment for the open Payment Request of a document.

	The amount is always read from the Payment Request, never from the caller, so
	whoever opens a payment page cannot influence what is charged.
	"""
	payment_request = get_open_payment_request(reference_doctype, reference_docname)
	controller = get_controller(payment_request)

	amount = amount_of(payment_request)
	currency = payment_request.currency

	intent = controller.create_payment_intent(
		amount=amount,
		currency=currency,
		description=payment_request.subject,
		email=payment_request.email_to,
		metadata=payment_metadata(payment_request),
		idempotency_key=f"{payment_request.name}-{to_stripe_amount(amount, currency)}",
	)

	return {
		"payment_intent": intent.id,
		"client_secret": intent.client_secret,
		"amount": amount,
		"currency": currency,
		"payment_request": payment_request.name,
	}


@frappe.whitelist(allow_guest=True)
def confirm_payment(payment_intent_id, payment_request=None):
	"""Browser callback after Stripe.js has confirmed a card payment."""
	reference = frappe.get_doc("Payment Request", payment_request) if payment_request else None
	controller = get_controller(reference)

	intent = controller.retrieve_payment_intent(payment_intent_id)
	result = controller.finalize_payment_intent(intent=intent)

	if not result.already_paid:
		log_intent(intent, source="browser")

	if not result.redirect_to:
		result.redirect_to = fallback_redirect(frappe.get_doc("Payment Request", result.payment_request))

	return result


@frappe.whitelist(allow_guest=True)
def stripe_webhook():
	"""Stripe webhook endpoint.

	Register ``https://<site>/api/method/payments.payment_gateways.stripe_api.stripe_webhook``
	in the Stripe dashboard for ``payment_intent.succeeded``,
	``payment_intent.amount_capturable_updated`` and ``payment_intent.canceled``
	(plus optionally ``payment_intent.payment_failed``). It exists because the
	browser callback is best-effort: the customer may close the tab, lose
	connectivity, or the payment method may settle asynchronously.

	``amount_capturable_updated`` matters when Stripe Settings is set to authorise
	only: it is the only signal that a card was successfully held, and it is what
	lets the order be placed even when the customer never makes it back from
	Stripe.js. The capture itself arrives later, as ``payment_intent.succeeded``.
	"""
	if frappe.request is None:
		return {"status": "ignored"}

	payload = frappe.request.get_data()
	controller = get_controller()
	controller.verify_webhook_signature(payload, frappe.get_request_header("Stripe-Signature"))

	try:
		event = json.loads(payload)
	except ValueError:
		frappe.throw(_("Malformed webhook payload."))

	event_type = event.get("type")
	intent_id = ((event.get("data") or {}).get("object") or {}).get("id")
	if not intent_id:
		return {"status": "ignored"}

	# ``succeeded`` is the money moving (immediately, or the moment a manual-capture
	# intent is captured from the Stripe dashboard). ``amount_capturable_updated``
	# is a card that has just been held instead. Both are finalised from the intent
	# re-read from Stripe, so neither can be faked by the caller.
	if event_type in ("payment_intent.succeeded", "payment_intent.amount_capturable_updated"):
		try:
			intent = controller.retrieve_payment_intent(intent_id)
			result = controller.finalize_payment_intent(intent=intent)
			if not result.already_paid:
				log_intent(intent, source="webhook")
			return result
		except Exception:
			# Let Stripe retry: finalisation is idempotent, so a retry is safe and
			# is exactly what we want if the failure was transient.
			frappe.log_error(
				title="Stripe webhook: finalisation failed",
				message=f"{intent_id}\n{frappe.get_traceback()}",
			)
			raise

	if event_type == "payment_intent.canceled":
		# A hold that will never be captured - released by us when the customer
		# declined, or by Stripe when the authorisation window lapsed. The order must
		# stop behaving as if the card were still held, or it can never be paid for.
		try:
			intent = controller.retrieve_payment_intent(intent_id)
			released = mark_authorization_released(intent, status="canceled")
			return {"status": "released", "payment_request": released}
		except Exception:
			# Not retried: the housekeeping job is the safety net, and a failure here
			# is usually permanent (the document is gone), so a retry would just loop.
			frappe.log_error(
				title="Stripe webhook: could not release a card authorisation",
				message=f"{intent_id}\n{frappe.get_traceback()}",
			)
			return {"status": "error"}

	if event_type == "payment_intent.payment_failed":
		frappe.log_error(
			title="Stripe webhook: payment_intent.payment_failed",
			message=json.dumps(
				{
					"payment_intent": intent_id,
					"metadata": ((event.get("data") or {}).get("object") or {}).get("metadata"),
				},
				indent=1,
				default=str,
			),
		)
		return {"status": "logged"}

	return {"status": "ignored"}
