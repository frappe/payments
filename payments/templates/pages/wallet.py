import json

import frappe
from frappe import _
from frappe.utils import flt
import grpc
from payments.payment_gateways.doctype.custom_payment_settings.databank import users_pb2, users_pb2_grpc, payments_pb2, payments_pb2_grpc
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
	print(reply)
	if reply.info.information=='200 OK':
		data={'info':reply.info.information,'balance':reply.balanceAfter}

	else:
		data={'info':reply.info.information}
	return data

def get_balance(reference_docname):
		gateway_controller = get_gateway_controller(reference_docname)
		channel=frappe.get_doc("Custom Payment Settings", gateway_controller).configure_wallet()
		domain_url=frappe.get_doc("Custom Payment Settings", gateway_controller).configure_domain()
		details= users_pb2.request(username=frappe.session.user, domain=domain_url)
		stub = users_pb2_grpc.userServiceStub(channel)
		response = stub.GetBalance(details)
		print(response)
		return response.balance
