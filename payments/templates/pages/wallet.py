import json

import frappe
from frappe import _
from frappe.utils import flt

from payments.payment_gateways.doctype.braintree_settings.braintree_settings import (
	get_client_token,
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
	"order_id",
	"currency",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]


		context["amount"] = flt(context["amount"])

		gateway_controller = get_gateway_controller(context.reference_docname)
		context["header_img"] = frappe.db.get_value(
			"Custom Payment Settings", gateway_controller, "header_img"
		)
		context['items']=get_order_items(context['reference_docname'])
	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect

def get_order_items(reqname):
	order=frappe.get_doc('Sales Order',(frappe.get_value('Payment Request',reqname,['reference_name'])), as_dict=1)
	return order.items
	
    
@frappe.whitelist(allow_guest=True)
def make_payment(payload_nonce, data, reference_doctype, reference_docname):
	data = json.loads(data)

	data.update({"payload_nonce": payload_nonce})

	gateway_controller = get_gateway_controller(reference_docname)
	data = frappe.get_doc("Custom Payment Settings", gateway_controller).create_payment_request(data)
	frappe.db.commit()
	return data
