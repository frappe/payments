# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
import json
from frappe.model.document import Document
from urllib.parse import urlencode

import xendit
from xendit.apis import BalanceApi
from pprint import pprint
from xendit.apis import InvoiceApi
from xendit.invoice.model.create_invoice_request import CreateInvoiceRequest
from xendit.invoice.model import CustomerObject, AddressObject, NotificationPreference, NotificationChannel, InvoiceItem, InvoiceFee, ChannelProperties, ChannelPropertiesCards
from datetime import datetime
from frappe.utils import call_hook_method, cint, flt, get_url

from payments.utils import create_payment_gateway


def serialize_datetime(obj):
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%d %H:%M:%S')
    raise TypeError("Type not serializable")

class XenditSettings(Document):

	supported_currencies = ["IDR"]

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Xendit does not support transactions in currency '{0}'"
				).format(currency)
			)

	def on_update(self):
		create_payment_gateway(
			"Xendit-" + self.title,
			settings="Xendit Settings",
			controller=self.title,
		)

	def get_invoice_by_id(self, invoice_id):
		xendit.set_api_key(self.secret_api_key)

		api_client = xendit.ApiClient()
		api_instance = InvoiceApi(api_client)
		try:
			# Get an invoice by ID
			api_response = api_instance.get_invoice_by_id(invoice_id)
			pprint(api_response)
		except xendit.XenditSdkException as e:
			print("Exception when calling InvoiceApi->get_invoice: %s\n" % e)


	def create_request(self,doc_type, document, payload):
		xendit.set_api_key(self.secret_api_key)

		api_client = xendit.ApiClient()
		api_instance = InvoiceApi(api_client)
		create_invoice_request = CreateInvoiceRequest(
				**payload
		)
		try:
			# Create an invoice
			api_response = api_instance.create_invoice(create_invoice_request)
			# import pdb
			# pdb.set_trace()
			xpl = frappe.new_doc("Xendit Payment Log")
			xpl.xendit_account = self.name
			xpl.respond_payload = json.dumps(api_response.to_dict(), default=serialize_datetime, indent=4)  #json.dumps(api_response.to_dict())
			xpl.doc_type = doc_type
			xpl.document = document
			xpl.status = api_response.status
			xpl.amount = api_response.amount
			xpl.checkout_url = api_response.invoice_url
			xpl.submit()
			frappe.db.commit()
			return xpl.checkout_url
			# pprint(api_response)
		except xendit.XenditSdkException as e:
			print("Exception when calling InvoiceApi->create_invoice: %s\n" % e)


	def get_payment_url(self, **kwargs):
		payload = {'external_id': kwargs['reference_docname'],
			'amount': kwargs['amount'],
			'payer_email': kwargs['payer_email'],
			'description':str(kwargs['description']),
			'currency': kwargs['currency']
		}
		checkout_url = self.create_request("Payment Request", kwargs['reference_docname'], payload)
		return get_url(checkout_url)

