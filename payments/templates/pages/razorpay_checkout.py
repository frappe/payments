# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from payments.utils.utils import validate_integration_request

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
	context.api_key = get_api_key()

	try:
		validate_integration_request(frappe.form_dict["token"])

		doc = frappe.get_doc("Integration Request", frappe.form_dict["token"])

		payment_details = json.loads(doc.data)

		if not doc.get("reference_docname") or not doc.get("reference_doctype"):
			redirect_to_checkout_message(
				_("Invalid Payment Request"),
				_("Reference doctype and document name are required."),
			)

		reference_docstatus = frappe.db.get_value(
			doc.get("reference_doctype"), doc.get("reference_docname"), "docstatus"
		)
		if reference_docstatus == 2:
			redirect_to_checkout_message(
				_("Payment Request Cancelled"),
				_("This payment request has been cancelled."),
				http_status_code=410,
			)

		for key in expected_keys:
			context[key] = payment_details[key]

		context["token"] = frappe.form_dict["token"]
		context["amount"] = flt(context["amount"])
		context["subscription_id"] = (
			payment_details["subscription_id"] if payment_details.get("subscription_id") else ""
		)

	except frappe.Redirect:
		raise
	except Exception:
		redirect_to_checkout_message(
			_("Invalid Token"),
			_("Seems token you are using is invalid!"),
		)


def redirect_to_checkout_message(
	title,
	message,
	http_status_code=400,
	indicator_color="red",
	context=None,
):
	frappe.redirect_to_message(
		title,
		message,
		http_status_code=http_status_code,
		context=context,
		indicator_color=indicator_color,
	)
	frappe.local.flags.redirect_location = frappe.local.response.location
	raise frappe.Redirect


def get_api_key():
	api_key = frappe.db.get_single_value("Razorpay Settings", "api_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		api_key = frappe.conf.sandbox_api_key

	return api_key


@frappe.whitelist(allow_guest=True)
def make_payment(razorpay_payment_id, options, reference_doctype, reference_docname, token):
	data = {}

	if isinstance(options, str):
		data = json.loads(options)

	data.update(
		{
			"razorpay_payment_id": razorpay_payment_id,
			"reference_docname": reference_docname,
			"reference_doctype": reference_doctype,
			"token": token,
		}
	)

	data = frappe.get_doc("Razorpay Settings").create_request(data)
	frappe.db.commit()
	return data
