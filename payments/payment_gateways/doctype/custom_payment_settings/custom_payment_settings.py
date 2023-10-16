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
		channel= grpc.insecure_channel(self.url)

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Wallet does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		return get_url(f"./wallet?{urlencode(kwargs)}")





