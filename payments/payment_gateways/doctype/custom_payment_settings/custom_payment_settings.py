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
from .databank import payments_pb2, payments_pb2_grpc
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
	
	def configure_domain(self):
		return self.domain_url


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
		return self.create_charge_on_wallet()
		
		

	def create_charge_on_wallet(self):
		channel= self.configure_wallet()		
		domain=self.configure_domain()

		try:

			details= payments_pb2.requests(username=frappe.session.user, domain=domain, amount=float(self.data.amount), balanceType='*monetary')
			stub = payments_pb2_grpc.paymentsServiceStub(channel)
			response = stub.DebitAccount(details)
			print(response.info.information)
			if response.info.information=='200 OK':
				custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", 'Completed')
				return response
		
			elif response.error.localizedDescription=='Insufficient funds!':
				frappe.throw('Insufficient funds!')
			else:
				frappe.throw("Service Down")
		except grpc.RpcError as e:
			frappe.throw(f"{e.code()}")


def get_gateway_controller(doc):
	payment_request = frappe.get_doc("Payment Request", doc)
	gateway_controller = frappe.db.get_value(
		"Payment Gateway", payment_request.payment_gateway, "gateway_controller"
	)
	return gateway_controller

