# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
import stripe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import cint, flt

# Stripe expects the amount in the smallest currency unit. These currencies have
# no minor unit, so the amount is passed through unchanged.
ZERO_DECIMAL_CURRENCIES = frozenset(
	{
		"BIF",
		"CLP",
		"DJF",
		"GNF",
		"JPY",
		"KMF",
		"KRW",
		"MGA",
		"PYG",
		"RWF",
		"UGX",
		"VND",
		"VUV",
		"XAF",
		"XOF",
		"XPF",
	}
)

# Stripe's status for a manual-capture authorisation that is holding the money on
# the customer's card without taking it.
PENDING_CAPTURE_INTENT_STATUS = "requires_capture"

# Hook status handed to ``on_payment_authorized`` for such an authorisation. It is
# deliberately *not* "Authorized": that is what gateways like Razorpay report when
# the money is on its way, and apps record those as paid.
PENDING_CAPTURE = "Pending Capture"


def create_stripe_subscription(gateway_controller, data):
	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	stripe_settings.data = frappe._dict(data)

	stripe.api_key = stripe_settings.get_password(fieldname="secret_key", raise_exception=False)
	stripe.default_http_client = stripe.http_client.RequestsClient()

	try:
		stripe_settings.integration_request = create_request_log(stripe_settings.data, "Host", "Stripe")
		stripe_settings.payment_plans = frappe.get_doc(
			"Payment Request", stripe_settings.data.reference_docname
		).subscription_plans
		return create_subscription_on_stripe(stripe_settings)

	except Exception:
		stripe_settings.log_error("Unable to create Stripe subscription")
		return {
			"redirect_to": frappe.redirect_to_message(
				_("Server Error"),
				_(
					"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
				),
			),
			"status": 401,
		}


def create_subscription_on_stripe(stripe_settings):
	items = []
	for payment_plan in stripe_settings.payment_plans:
		plan = frappe.db.get_value("Subscription Plan", payment_plan.plan, "product_price_id")
		items.append({"price": plan, "quantity": payment_plan.qty})

	try:
		customer = stripe.Customer.create(
			source=stripe_settings.data.stripe_token_id,
			description=stripe_settings.data.payer_name,
			email=stripe_settings.data.payer_email,
		)

		subscription = stripe.Subscription.create(customer=customer, items=items)

		if subscription.status == "active":
			stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
			stripe_settings.flags.status_changed_to = "Completed"

		else:
			stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
			frappe.log_error(f"Stripe Subscription ID {subscription.id}: Payment failed")
	except Exception:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		stripe_settings.log_error("Unable to create Stripe subscription")

	return stripe_settings.finalize_request()


# ---------------------------------------------------------------------------
# PaymentIntents
#
# Card payments are two-phase: an intent is created for an amount, the browser
# confirms it with Stripe.js (that is where 3-D Secure happens), and the result
# is only acted upon after it has been read back from Stripe. The webhook in
# ``stripe_api`` exists because the browser half is best-effort.
# ---------------------------------------------------------------------------


def get_stripe_api(controller):
	"""Point the Stripe module at the gateway controller's secret key."""
	secret_key = controller.get_password(fieldname="secret_key", raise_exception=False)
	if not secret_key:
		frappe.throw(_("Stripe is not configured."))

	stripe.api_key = secret_key
	stripe.default_http_client = stripe.http_client.RequestsClient()
	return stripe


def get_capture_method(controller):
	"""``manual`` when the account authorises only, else ``automatic``.

	Tolerant of a controller without the field, so this also works against a
	Stripe Settings record that has not been migrated yet - which must behave the
	way it always did: charge immediately.
	"""
	return "manual" if controller.get("manual_capture") else "automatic"


def to_stripe_amount(amount, currency):
	"""Convert an amount to Stripe's smallest-unit integer."""
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return cint(round(flt(amount)))

	return cint(round(flt(amount) * 100))


def create_payment_intent(
	controller,
	amount,
	currency,
	description=None,
	email=None,
	metadata=None,
	idempotency_key=None,
	capture_method=None,
):
	"""Create (or re-fetch) a PaymentIntent for ``amount`` in ``currency``.

	An ``idempotency_key`` makes a retried call return the *same* intent instead of
	raising a second one for the same payment. Include the amount in it, so that an
	edited basket gets a fresh intent rather than the old amount.

	``capture_method`` defaults to the account's ``Stripe Settings`` - ``manual``
	leaves the money on the card untouched until the intent is captured, which is
	what "approve the payment in Stripe first" needs.
	"""
	stripe_api = get_stripe_api(controller)

	params = {
		"amount": to_stripe_amount(amount, currency),
		"currency": (currency or "").lower(),
		"payment_method_types": ["card"],
	}

	# Only send the parameter when it differs from Stripe's default, so an account
	# that charges immediately keeps sending exactly the request it always did.
	if (capture_method or get_capture_method(controller)) == "manual":
		params["capture_method"] = "manual"

	if description:
		params["description"] = description
	if email:
		params["receipt_email"] = email
	if metadata:
		params["metadata"] = metadata

	request_options = {"idempotency_key": idempotency_key} if idempotency_key else {}

	try:
		return stripe_api.PaymentIntent.create(**params, **request_options)
	except Exception:
		frappe.log_error(
			title="Stripe: could not create a PaymentIntent",
			message=f"{frappe.as_json(params)}\n{frappe.get_traceback()}",
		)
		frappe.throw(_("We could not start the payment with the payment provider."))


def retrieve_payment_intent(controller, payment_intent_id):
	"""Read a PaymentIntent back from Stripe.

	This is the source of truth for whether a payment happened: neither the browser
	nor a webhook payload is trusted for the status.
	"""
	if not payment_intent_id:
		frappe.throw(_("Missing payment reference."))

	stripe_api = get_stripe_api(controller)
	try:
		return stripe_api.PaymentIntent.retrieve(payment_intent_id)
	except Exception:
		frappe.log_error(
			title="Stripe: could not retrieve a PaymentIntent",
			message=f"{payment_intent_id}\n{frappe.get_traceback()}",
		)
		frappe.throw(_("We could not verify this payment with the payment provider."))


def verify_webhook_signature(controller, payload, signature):
	"""Verify a webhook's Stripe signature, when a signing secret is configured.

	A missing secret is not a hole: callers take only the intent id from the
	payload and re-read the intent from Stripe, so a forged payload cannot mark an
	unpaid document as paid. It is logged rather than silently ignored.
	"""
	secret = controller.get_password(fieldname="webhook_secret", raise_exception=False)
	if not secret:
		frappe.logger("payments").warning(
			"Stripe webhook received, but no Webhook Signing Secret is set on Stripe Settings %s"
			" - the signature was not verified.",
			controller.name,
		)
		return

	try:
		stripe.Webhook.construct_event(payload, signature, secret)
	except Exception:
		frappe.log_error(
			title="Stripe webhook: signature verification failed",
			message=frappe.get_traceback(),
		)
		frappe.throw(_("Invalid webhook signature."), frappe.AuthenticationError)


def finalize_payment_intent(controller, payment_intent_id=None, intent=None):
	"""Act on a PaymentIntent that Stripe has come back with.

	Two outcomes are accepted, and they are *not* the same thing:

	* ``succeeded`` - the money has moved. The Payment Request is submitted and the
	  reference document's ``on_payment_authorized("Completed")`` hook runs.
	* ``requires_capture`` - a manual-capture authorisation. The card is only held,
	  so nothing is recorded as paid; the hook runs with ``PENDING_CAPTURE`` instead
	  so the app can place the order, and the payment is recorded later when
	  capturing in the Stripe dashboard fires ``payment_intent.succeeded``.

	Idempotent: the browser callback and the webhook routinely race, and Stripe
	retries webhooks, so the Payment Request may already be paid by the time we get
	here.

	Returns ``redirect_to``, ``payment_request``, ``already_paid`` and
	``authorized``.
	"""
	intent = intent or retrieve_payment_intent(controller, payment_intent_id)
	status = intent.get("status")
	authorized = status == PENDING_CAPTURE_INTENT_STATUS
	if status != "succeeded" and not authorized:
		frappe.throw(_("Payment has not been completed."), title=_("Payment Failed"))

	payment_request = (intent.get("metadata") or {}).get("payment_request")
	if not payment_request:
		frappe.throw(_("This payment is not linked to a payment request."))

	pr = frappe.get_doc("Payment Request", payment_request)

	if pr.status == "Paid":
		return frappe._dict(
			redirect_to=None, payment_request=pr.name, already_paid=True, authorized=False
		)

	# The hook may want the audit trail, and the reference document often cannot be
	# paid against until it has been submitted.
	pr.flags.payment_intent_id = intent.get("id")

	if authorized:
		# The money is still on the customer's card: submitting the Payment Request
		# here would post a Payment Entry for funds that were never collected.
		return frappe._dict(
			redirect_to=run_on_payment_authorized(pr, PENDING_CAPTURE),
			payment_request=pr.name,
			already_paid=False,
			authorized=True,
		)

	if pr.docstatus == 0:
		pr.flags.ignore_permissions = True
		pr.flags.mute_email = True
		pr.submit()

	return frappe._dict(
		redirect_to=run_on_payment_authorized(pr, "Completed"),
		payment_request=pr.name,
		already_paid=False,
		authorized=False,
	)


def run_on_payment_authorized(payment_request, status):
	"""Let the app say what a payment means for its documents.

	``status`` is either ``"Completed"`` (the money moved), one of the other
	statuses a gateway reports as "we will get paid" (Razorpay's ``"Authorized"``,
	for instance), or ``PENDING_CAPTURE`` for a manual-capture hold where no money
	has moved at all.

	Every gateway in this app expects an ``on_payment_authorized`` method on the
	*reference* document, and that is the fallback here. A Payment Request may
	provide its own instead - the webshop does, because the document that has to be
	submitted first is the draft Sales Order behind the request.
	"""
	hook = getattr(payment_request, "on_payment_authorized", None)
	if hook:
		return hook(status)

	reference = frappe.get_doc(payment_request.reference_doctype, payment_request.reference_name)
	return reference.run_method("on_payment_authorized", status)
