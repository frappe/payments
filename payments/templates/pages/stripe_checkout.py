# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
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
	"order_id",
	"currency",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]

		gateway_controller = get_gateway_controller(context.reference_doctype, context.reference_docname)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)
		context["payment_id"] = frappe.db.get_value(context.reference_docname, context.reference_doctype, "payment_id")
		context.payment_id = frappe.db.get_value(context.reference_docname, context.reference_doctype, "payment_id")

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

		if is_a_subscription(context.reference_doctype, context.reference_docname):
			payment_plan = frappe.db.get_value(
				context.reference_doctype, context.reference_docname, "payment_plan"
			)
			recurrence = frappe.db.get_value("Payment Plan", payment_plan, "recurrence")

			context["amount"] = context["amount"] + " " + _(recurrence)

	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key(doc, gateway_controller):
	publishable_key = frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		publishable_key = frappe.conf.sandbox_publishable_key

	return publishable_key


def get_header_image(doc, gateway_controller):
	header_image = frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")

	return header_image


@frappe.whitelist(allow_guest=True)
def create_payment(data, reference_doctype=None, reference_docname=None):
	data = json.loads(data)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname)
	data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)
	frappe.db.commit()
	return data['client_secret']



@frappe.whitelist(allow_guest=True)
def make_payment(data, reference_doctype=None, reference_docname=None):
	data = json.loads(data)
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname)
	payment_id = frappe.get_value(reference_doctype, reference_docname, 'payment_id')
	#frappe.get_doc(reference_doctype, reference_docname).run_method('on_payment_authorized', 'Completed')
	finalize = frappe.get_doc("Stripe Settings", gateway_controller).validate_payment(data, payment_id)
	frappe.db.commit()
	return finalize


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")
