# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import fmt_money

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
	key_mode,
	major_units,
	not_recorded_location,
)

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"currency",
	"payment_gateway",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]
		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		if not gateway_controller:
			redirect_to_message(
				_("Payment not available"), _("This payment link isn't valid. Please ask for a new one.")
			)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.test_mode = key_mode(context.publishable_key) == "test"
		context.image = get_header_image(context.reference_docname, gateway_controller)

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

		if is_a_subscription(context.reference_doctype, context.reference_docname):
			payment_plan = frappe.db.get_value(
				context.reference_doctype, context.reference_docname, "payment_plan"
			)
			recurrence = frappe.db.get_value("Payment Plan", payment_plan, "recurrence")

			context["amount"] = context["amount"] + " " + _(recurrence)
		elif context.reference_doctype == "Payment Request":
			# a one-off payment: a PaymentIntent for what the request asks, so the bank can
			# authenticate the payer (3-D Secure) and wallets work. Only a Payment Request, which is
			# made to be paid by whoever has its link, is looked up from the link.
			if frappe.db.get_value(context.reference_doctype, context.reference_docname, "docstatus") == 2:
				redirect_to_message(
					_("Payment not available"), _("This payment was cancelled, so it can't be paid.")
				)
			# ERPNext's link carries the email the request is sent to, and the intent shown here
			# carries it as the receipt email: a visitor who only guessed the request's name is
			# turned away rather than shown it
			payer_email = frappe.db.get_value(
				context.reference_doctype, context.reference_docname, "email_to"
			)
			if payer_email and payer_email.strip().lower() != (context.payer_email or "").strip().lower():
				redirect_to_message(
					_("Payment not available"), _("This payment link isn't valid. Please ask for a new one.")
				)
			settings = frappe.get_doc("Stripe Settings", gateway_controller)
			try:
				intent = open_payment_intent(settings, context.reference_doctype, context.reference_docname)
			except frappe.ValidationError as e:
				frappe.clear_messages()
				redirect_to_message(
					_("Payment not available"), str(e) or _("There's nothing left to pay here.")
				)
			if intent.status == "succeeded":
				# paid already (the payer came back to this page): recorded once, then on as after paying.
				# Kept only once recorded: if recording fails, nothing is kept and the webhook tries again
				outcome = settings.record_payment_intent(intent)
				frappe.local.flags.commit = True
				frappe.local.flags.redirect_location = (
					outcome.location if outcome else not_recorded_location()
				)
				raise frappe.Redirect(302)
			if intent.status == "processing":
				redirect_to_message(
					_("Payment processing"),
					_("Your payment is processing. You'll get an email once it's confirmed."),
				)
			context.client_secret = intent.client_secret
			# what the intent asks (the request's outstanding amount), with the code, as many
			# currencies share a symbol
			currency = intent.currency.upper()
			money = fmt_money(amount=major_units(intent.amount, currency), currency=currency)
			context["amount"] = f"{money} {currency}"

	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")


def redirect_to_message(title: str, message: str):
	frappe.redirect_to_message(title, message)
	frappe.local.flags.redirect_location = frappe.local.response.location
	raise frappe.Redirect(302)


# the pay page is open to guests and makes a PaymentIntent
@rate_limit(limit=30, seconds=60 * 10)
def open_payment_intent(settings, reference_doctype: str, reference_docname: str):
	return settings.payment_intent(reference_doctype, reference_docname)


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")


# Guest checkout: buyers pay without a site account.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
@rate_limit(limit=10, seconds=60 * 10)
def make_payment(
	stripe_token_id: str,
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	data = json.loads(data)

	data.update({"stripe_token_id": stripe_token_id})

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)

	if is_a_subscription(reference_doctype, reference_docname):
		reference = frappe.get_doc(reference_doctype, reference_docname)
		data = reference.create_subscription("stripe", gateway_controller, data)
	else:
		data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)

	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")


# Guest checkout: the payer returns here from Stripe without a site account.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
@rate_limit(limit=20, seconds=60)
def confirm_payment(
	payment_intent: str, reference_doctype: str, reference_docname: str, payment_gateway: str
):
	"""After Stripe confirms a payment in the browser: check the PaymentIntent with Stripe and record
	the payment against its document."""
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	if not gateway_controller:
		raise frappe.PermissionError
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	intent = settings.client().payment_intents.retrieve(payment_intent)
	metadata = intent.metadata or {}
	if (metadata.get("reference_doctype"), metadata.get("reference_docname")) != (
		reference_doctype,
		reference_docname,
	):
		raise frappe.PermissionError
	if intent.status == "succeeded":
		outcome = settings.record_payment_intent(intent)
		# reachable as a guest, and the payment must be recorded
		frappe.db.commit()  # nosemgrep
		if outcome and outcome.recorded:
			return {"status": "Completed", "redirect_to": outcome.location}
		return {
			"status": "Not Recorded",
			"redirect_to": outcome.location if outcome else not_recorded_location(),
		}
	if intent.status == "processing":
		return {
			"status": "Processing",
			"message": _("Your payment is processing. You'll get an email once it's confirmed."),
		}
	return {
		"status": "Failed",
		"message": _("The payment didn't go through. Please try again or use another card."),
	}
