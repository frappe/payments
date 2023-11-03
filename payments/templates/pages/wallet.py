import json

import frappe
from frappe import _
from frappe.utils import flt
import grpc
from payments.payment_gateways.doctype.custom_payment_settings.databank import wallet_pb2, wallet_pb2_grpc
from payments.payment_gateways.doctype.custom_payment_settings.custom_payment_settings import (
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
		context['balance']=get_balance(context["reference_docname"])
		context['pred_balance']= context["balance"]-context['amount']

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
	
def get_balance():
	pass


@frappe.whitelist(allow_guest=True)
def cancel_payment(data):
	try:
		data = json.loads(data)
		print(data['order_id'])
		payment=frappe.get_doc('Payment Request', data['order_id'])
		payment.cancel()
		return 'Success'
	except Exception:
		frappe.throw('Payment Already Cancelled')

@frappe.whitelist(allow_guest=True)
def make_payment(data, reference_doctype, reference_docname):
	data = json.loads(data)
	gateway_controller = get_gateway_controller(reference_docname)
	reply = frappe.get_doc("Custom Payment Settings", gateway_controller).create_payment_request(data)
	frappe.db.commit()
	if reply.info=='Transaction successful':
		data={'info':reply.info,'balance':reply.monetary}

	else:
		data={'info':reply.info}
	return data

def get_balance(reference_docname):
	print(reference_docname)
	try:
		gateway_controller = get_gateway_controller(reference_docname)
		channel=frappe.get_doc("Custom Payment Settings", gateway_controller).configure_wallet()
		details= wallet_pb2.user(username='kelvin zawala')
		stub = wallet_pb2_grpc.walletStub(channel)
		response = stub.balance(details)
		print(response)
		return response.monetary
	except grpc.RpcError as e:
		frappe.redirect_to_message(
			_("An Error occurred"),
			_(f"{e.code()}"),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect
			