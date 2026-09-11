# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import fmt_money

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
)
from payments.payment_gateways.stripe_api import amount_of, get_open_payment_request

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
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)

		context.is_subscription = is_a_subscription(
			context.reference_doctype, context.reference_docname
		)

		context["amount"] = fmt_money(
			amount=get_amount_to_pay(context.reference_doctype, context.reference_docname),
			currency=context["currency"],
		)

		if context.is_subscription:
			payment_plan = frappe.db.get_value(
				context.reference_doctype, context.reference_docname, "payment_plan"
			)
			recurrence = frappe.db.get_value("Payment Plan", payment_plan, "recurrence")

			context["amount"] = context["amount"] + " " + _(recurrence)
	else:
		frappe.log_error(
			"Missing keys in form_dict",
			f"Expected keys: {expected_keys},Received keys: {list(frappe.form_dict)}",
		)
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key(doc, gateway_controller):
	# No sandbox override: the PaymentIntent (or the subscription mandate) is always
	# created with the keys on Stripe Settings, so a swapped publishable key could
	# never match the secret key.
	return frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")


def get_amount_to_pay(reference_doctype, reference_docname):
	"""The amount actually due, read from the payment request.

	It used to be taken from the query string, so anyone opening this page could
	choose what to pay — for example 0.01 — while the gateway still marked the
	whole document as paid. This fails closed instead.
	"""
	payment_request = get_open_payment_request(reference_doctype, reference_docname)
	return amount_of(payment_request)


# nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
@frappe.whitelist(allow_guest=True)
def make_payment(
	stripe_token_id: str,
	data: str,
	reference_doctype: str | None = None,
	reference_docname: str | None = None,
	payment_gateway: str | None = None,
):
	data = json.loads(data)

	data.update({"stripe_token_id": stripe_token_id})

	# One-off card payments go through the PaymentIntents endpoints in
	# ``stripe_api`` instead, because those are the ones 3-D Secure works with.
	# This endpoint survives only for subscriptions, which need a card token to set
	# up the recurring mandate — and it refuses to charge anything else.
	if not is_a_subscription(reference_doctype, reference_docname):
		frappe.throw(_("This payment cannot be made with a card token."))

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	reference = frappe.get_doc(reference_doctype, reference_docname)
	data = reference.create_subscription("stripe", gateway_controller, data)

	frappe.db.commit()
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")
