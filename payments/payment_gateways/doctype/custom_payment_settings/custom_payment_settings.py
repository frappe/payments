# Copyright (c) 2023, Frappe Technologies and contributors
# For license information, please see license.txt

from urllib.parse import urlencode

import braintree
import frappe
from frappe import _
import grpc
#from . import payments
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url
import grpc
from .databank import wallet_pb2, wallet_pb2_grpc
from payments.utils import create_payment_gateway

class CustomPaymentSettings(Document):
	supported_currencies = [
		"USD",
		"ZWL"
	]

	def validate(self):
		if not self.flags.ignore_mandatory:
			self.configure_wallet()


	def configure_wallet(self):
		return grpc.insecure_channel(self.url)


	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Wallet does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		return get_url(f"./wallet?{urlencode(kwargs)}")
	
	def create_payment_request(self, data):
		self.data = frappe._dict(data)
		
		try:
			return self.create_charge_on_wallet()
		except Exception:
			frappe.log_error('Grpc error')
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"There seems to be an issue with the server's Wallet configuration. Don't worry, in case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}
		

	def create_charge_on_wallet(self):
		channel= self.configure_wallet()
		details= wallet_pb2.user(username='kelvin zawala',monetary=100)
		stub = wallet_pb2_grpc.walletStub(channel)
		response = stub.debit(details)
		if response.info=='Transaction successful':
			custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", 'Completed')
			print(custom_redirect_to)
			return response
		elif response.info=='Insufficient funds':
			return response
		else:
			frappe.throw(response.info)

	def get_balance():
		pass

def get_gateway_controller(doc):
	payment_request = frappe.get_doc("Payment Request", doc)
	gateway_controller = frappe.db.get_value(
		"Payment Gateway", payment_request.payment_gateway, "gateway_controller"
	)
	return gateway_controller