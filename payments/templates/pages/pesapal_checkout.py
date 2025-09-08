# Copyright (c) 2024, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.utils import flt

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
	
	try:
		validate_integration_request(frappe.form_dict["token"])
		
		doc = frappe.get_doc("Integration Request", frappe.form_dict["token"])
		payment_details = json.loads(doc.data)
		
		for key in expected_keys:
			context[key] = payment_details.get(key)
		
		context["token"] = frappe.form_dict["token"]
		context["amount"] = flt(context["amount"])
		
		# Get Pesapal settings
		pesapal_settings = frappe.get_doc("Pesapal Settings")
		
		# Create payment request
		response = pesapal_settings.create_request({"token": context["token"]})
		
		if response.get("redirect_to"):
			# Redirect to Pesapal payment page
			frappe.local.flags.redirect_location = response["redirect_to"]
			raise frappe.Redirect
		else:
			# Show error page
			frappe.redirect_to_message(
				_("Payment Error"),
				_("Unable to process payment request. Please try again."),
				http_status_code=400,
				indicator_color="red",
			)
			frappe.local.flags.redirect_location = frappe.local.response.location
			raise frappe.Redirect
			
	except frappe.Redirect:
		raise
	except Exception:
		frappe.log_error(frappe.get_traceback())
		frappe.redirect_to_message(
			_("Invalid Token"),
			_("Seems token you are using is invalid!"),
			http_status_code=400,
			indicator_color="red",
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


@frappe.whitelist(allow_guest=True)
def make_payment(order_tracking_id, merchant_reference, token):
	"""Handle payment completion callback"""
	try:
		# Get Pesapal settings
		pesapal_settings = frappe.get_doc("Pesapal Settings")
		
		# Process the payment callback
		pesapal_settings.process_payment_callback(order_tracking_id, merchant_reference)
		
		# Get integration request
		integration_request = frappe.get_doc("Integration Request", token)
		data = json.loads(integration_request.data)
		
		# Return success response
		return {
			"redirect_to": f"payment-success?doctype={data.get('reference_doctype')}&docname={data.get('reference_docname')}",
			"status": 200
		}
		
	except Exception:
		frappe.log_error(frappe.get_traceback())
		return {
			"redirect_to": "payment-failed",
			"status": 400
		}
