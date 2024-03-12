from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, cint, flt, get_url
from payments.utils import create_payment_gateway
from mollie.api.client import Client
from mollie.api.error import Error

mollie_client = Client()
mollie_error = Error()

class MollieSettings(Document):
	supported_currencies = [
		"AED",
		"AUD",
		"BGN",
		"BRL",
		"CAD",
		"CHF",
		"CZK",
		"DKK",
		"EUR",
		"GBP",
		"HKD",
		"HUF",
		"ILS",
		"ISK",
		"JPY",
		"MXN",
		"MYR",
		"NOK",
		"NZD",
		"PHP",
		"PLN",
		"RON",
		"RUB",
		"SEK",
		"SGD",
		"THB",
		"TWD",
		"USD",
		"ZAR",
	]

	def on_update(self):
		create_payment_gateway(
			"Mollie-" + self.gateway_name,
			settings="Mollie Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Mollie-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_mollie_credentials()

	def validate_mollie_credentials(self):
		if self.profile_id and self.secret_key:
			header = {
				"Authorization": "Bearer {}".format(
					self.get_password(fieldname="secret_key", raise_exception=False)
				)
			}
			try:
				make_get_request(url="https://api.mollie.com/v2/payments", headers=header)
			except Exception:
				frappe.throw(_("Seems Publishable Key or Secret Key is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Mollie does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_payment_url(self, **kwargs):
		return get_url(f"mollie_checkout?{urlencode(kwargs)}")

	def create_request(self, data):
		self.data = frappe._dict(data)
		api = mollie_client.set_api_key(self.get_password(fieldname="secret_key", raise_exception=False))

		try:
			self.integration_request = create_request_log(self.data, service_name="Mollie")
			return self.create_charge_on_mollie()

		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's Mollie configuration. In case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def check_request(self, data, paymentID):
		mollie_client.set_api_key(self.get_password(fieldname="secret_key", raise_exception=False))
		try:
			payment = mollie_client.payments.get(paymentID)
			paymentUrl = "Unavailable"
		
			if payment.is_paid():
				status = "Completed"
			elif payment.is_pending():
				status = "Pending"
				paymentUrl = payment['_links']['checkout']['href']
			elif payment.is_open():
				status = "Open"
				paymentUrl = payment['_links']['checkout']['href']
			else:
				status = "Cancelled"
			
			return {"paymentUrl": paymentUrl, "status": status}

		except Exception:
			frappe.log_error(frappe.get_traceback())
			return f"API call failed"

	def create_charge_on_mollie(self):
		try:
			data_details = {
					"amount": self.data.amount,
					"title": f"Payment for {self.data.reference_doctype} {self.data.reference_docname}",
					"description": f"Payment for {self.data.reference_doctype} {self.data.reference_docname}",
					"reference_doctype": self.data.reference_doctype,
					"reference_docname": self.data.reference_docname,
					"payer_email": frappe.session.user,
					"payer_name": frappe.utils.get_fullname(frappe.session.user),
					"order_id": self.data.reference_docname,
					"currency": self.data.currency,
					"redirect_to": self.data.get("redirect_to"),
				}
			redirect_url = self.get_payment_url(**data_details)
		
			charge = mollie_client.payments.create(
            		{
				'amount': {
        				'currency': self.data.currency,
        				'value': "{:.2f}".format(float(self.data.amount))
    				},
                		"description": self.data.description,
				'redirectUrl': redirect_url,
            			}
        		)

			frappe.db.set_value(self.data.reference_doctype, self.data.reference_docname, 'payment_id', charge.id)
		
		except Exception:
			if mollie_error:
				frappe.log_error(mollie_error)
			
			frappe.log_error(frappe.get_traceback())

		data2 = self.finalize_request()
		data2.update(paymentID=charge.id)
		data2.update(paymentUrl=charge.checkout_url)
		
		return data2

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					custom_redirect_to = frappe.get_doc(
						self.data.reference_doctype, self.data.reference_docname
					).run_method("on_payment_authorized", self.flags.status_changed_to)
				except Exception:
					frappe.log_error(frappe.get_traceback())

				if custom_redirect_to:
					redirect_to = custom_redirect_to

				redirect_url = "payment-success?doctype={}&docname={}".format(
					self.data.reference_doctype, self.data.reference_docname
				)

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
		else:
			redirect_url = "payment-failed"

		if redirect_to and "?" in redirect_url:
			redirect_url += "&" + urlencode({"redirect_to": redirect_to})
		else:
			redirect_url += "?" + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doctype, docname):
	reference_doc = frappe.get_doc(doctype, docname)
	gateway_controller = frappe.db.get_value(
		"Payment Gateway", reference_doc.payment_gateway, "gateway_controller"
	)
	return gateway_controller
