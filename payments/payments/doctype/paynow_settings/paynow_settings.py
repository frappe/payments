# Copyright (c) 2026, DonnC and contributors
# For license information, please see license.txt

import json
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.document import Document

from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import erpnext_app_import_guard, create_payment_gateway

from payments.payment_gateways.paynow import logger, Paynow, PaynowConfig, PaymentResponse, Payment, CartItem, PaymentStatus

IR_STATUS_MAPPER = {
		PaymentStatus.PAID: "Completed",
		PaymentStatus.AWAITING_DELIVERY: "Queued",
		PaymentStatus.DELIVERED: "Authorized",
		PaymentStatus.CREATED: "Queued",
		PaymentStatus.SENT: "Queued",
		PaymentStatus.CANCELLED: "Cancelled",
		PaymentStatus.DISPUTED: "Failed",
		PaymentStatus.REFUNDED: "Cancelled",
}

def create_mode_of_payment(gateway, payment_type="General"):
	with erpnext_app_import_guard():
		from erpnext import get_default_company

	payment_gateway_account = frappe.db.get_value(
		"Payment Gateway Account", {"payment_gateway": gateway}, ["payment_account"]
	)

	mode_of_payment = frappe.db.exists("Mode of Payment", gateway)
	if not mode_of_payment and payment_gateway_account:
		mode_of_payment = frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": gateway,
				"enabled": 1,
				"type": payment_type,
				"accounts": [
					{
						"doctype": "Mode of Payment Account",
						"company": get_default_company(),
						"default_account": payment_gateway_account,
					}
				],
			}
		)
		mode_of_payment.insert(ignore_permissions=True)

		return mode_of_payment
	elif mode_of_payment:
		return frappe.get_doc("Mode of Payment", mode_of_payment)

class PaynowSettings(Document):
	# status_callback_endpoint = "/api/method/payments.payment_gateways.doctype.paynow_settings.paynow_settings.paynow_callback"
	# redirect_callback_endpoint = "/api/method/payments.payment_gateways.doctype.paynow_settings.paynow_settings.paynow_return_manager"
	status_callback_endpoint = "/api/method/payments.payments.doctype.paynow_settings.paynow_settings.paynow_callback"
	redirect_callback_endpoint = "/api/method/payments.payments.doctype.paynow_settings.paynow_settings.paynow_return_manager"
	
	supported_currencies = ["USD", "ZWG"]

	def on_update(self):
		create_payment_gateway(
			"Paynow",
			settings="Paynow Settings",
			controller=self.name
		)
		create_mode_of_payment("Paynow")
		call_hook_method("payment_gateway_enabled", gateway="Paynow")

		frappe.db.commit()

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Paynow does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_customer_details(self, kwargs: dict):
		email = kwargs.get("payer_email", frappe.session.user)

		details = {
			"email": self.account_email if self.in_test_mode == 1 else email,
			"name": kwargs.get("payer_name") or frappe.utils.get_fullname(email) or "Guest",
			"phone": None,
		}

		if self.append_customer_details == 0:
			return details

		customer = frappe.db.get_value("Contact", {"email_id": email}, ["mobile_no"], as_dict=True)

		if not customer:
			customer = frappe.db.get_value("User", {"email": email}, ["mobile_no"], as_dict=True)

		if customer:
			# TODO: verify zw phone number format
			details["phone"] = customer.mobile_no

		return details

	def get_integration_configs(self) -> tuple:
		"""
			Fetches configurations from this gateway
			and returns a list of PaynowConfig.
		"""
		paynow_configs = []
		callback_url = get_url(self.status_callback_endpoint)
		redirect_url = get_url(self.redirect_callback_endpoint)

		for row in self.integrations:
			config = PaynowConfig(
				integration_id=row.integration_id,
				integration_key=row.integration_key,
				currency=row.currency
			)
			paynow_configs.append(config)

		if len(paynow_configs) == 0:
			frappe.throw(_("No Paynow integration configurations found"))

		if self.in_test_mode == 1:
			callback_url = self.test_base_url + self.status_callback_endpoint

		return paynow_configs, callback_url, redirect_url

	def get_instance(self, merchant_ref=None) -> Paynow:
		configs, callback_url, redirect_url = self.get_integration_configs()
		redirect_url = redirect_url if merchant_ref is None else f"{redirect_url}?order_id={merchant_ref}"
		api = Paynow(
			config=configs,
			status_callback_url=callback_url,
			redirect_callback_url= redirect_url
		)

		logger.debug(f"Paynow obj, status_callback_url: {callback_url}, redirect_callback_url: {redirect_url}")

		return api

	def get_payment_url(self, **kwargs):
		payer_info = self.get_customer_details(kwargs)
		merchant_ref = merchant_trace = kwargs.get("order_id")
		api = self.get_instance(merchant_ref)

		payment = Payment(
			merchant_reference=merchant_ref,
			merchant_trace=merchant_trace,
			currency=kwargs.get("currency"),
			customer_email=payer_info.get("email"),
			customer_name=payer_info.get("name"),
			customer_phone=payer_info.get("phone"),
		)

		payment.add(CartItem(
			title=kwargs.get("description"),
			amount=float(kwargs.get("amount"))
		))

		response = api.initiate_web(payment)

		kwargs.update(
			{
				"paynow_init": response.upstream_data
			}
		)
		logger.debug(f"Paynow initiation response: {response}")

		if response.success and response.redirect_url:
			create_request_log(kwargs, service_name="Paynow", name=response.merchant_reference)
			return response.redirect_url
		else:
			frappe.log_error(title="Paynow Initiation Failed", message=f"Paynow Initiation Failed: {response}")
			frappe.throw(_("Error initiating Paynow payment request: {0}").format(response.message))

@frappe.whitelist(allow_guest=True)
def paynow_callback():
	"""A callback to handle transaction status updates from paynow.

	Based on status, update the Integration Request and the linked doctype payment status.

	Returns:
		a string "OK" upon successful processing of the callback
	"""
	data = frappe.request.form.to_dict()

	logger.debug(f"Paynow status callback: {data}")

	payment_response = PaymentResponse.from_raw(data)

	logger.debug(f"Parsed paynow status callback response: {payment_response}")

	ir = frappe.get_doc("Integration Request", payment_response.merchant_reference)
  
	logger.debug(f"Paynow status Integration Request: {ir.as_dict()}")

	if payment_response.is_paid:
		ref_doc = frappe.get_doc(ir.reference_doctype, ir.reference_docname)

		if ir.status != "Completed":
			ref_doc.run_method("on_payment_authorized", "Completed")
			ir.handle_success(payment_response.upstream_data)
			frappe.db.commit()
			logger.debug(f"Paynow payment marked as Completed for Integration Request: {ir.name}")
			return "OK"

	ir.update_status({"paynow_response": data}, IR_STATUS_MAPPER.get(payment_response.status, "Failed"))

	if payment_response.status == PaymentStatus.ERROR:
		ir.db_set("error", payment_response.message, update_modified=False)

	frappe.db.commit()

	logger.debug(f"Paynow({payment_response.status}) callback processing completed for Integration Request: {ir.name}")

	return "OK"

@frappe.whitelist(allow_guest=True)
def paynow_return_manager(order_id):
	"""Get the redirect url to go to when payment is processed on Paynow

	Args:
		order_id (str): Order ID / Merchant Reference of the processed transaction

	Returns:
		a redirect to the payment success page or appropriate page based on the transaction status
	"""
	data = frappe.request.form.to_dict()

	logger.debug(f"Paynow redirect data: {data}")

	try:
		settings_name = frappe.db.get_value("Paynow Settings", {}, "name")

		if not settings_name:
			frappe.throw(_("Paynow gateway settings not found."))

		settings = frappe.get_doc("Paynow Settings", settings_name)

		api: Paynow = settings.get_instance(order_id)

		ir = frappe.get_doc("Integration Request", order_id)

		logger.debug(f"Return callback, Integration Request: {ir.as_dict()}")

		data = json.loads(ir.data)
		paynow_init_data = data.get("paynow_init", {})

		if ir.status == "Completed":
			base_url = f"/payment-success"

		else:
			response = api.poll_url(paynow_init_data.get("pollurl", ""))

			if response.is_paid:
				base_url = f"/payment-success"

			elif response.status == PaymentStatus.CANCELLED:
				base_url = "/payment-cancelled"

			else:
				base_url = "/payment-failed"

		params = {
			"doctype": ir.reference_doctype,
			"docname": ir.reference_docname
		}

		if data.get("redirect_to"):
			params["redirect_to"] = data.get("redirect_to")

		redirect_url = f"{base_url}?{urlencode(params)}"
		logger.debug(f"Paynow ReturnCallback redirecting to: {redirect_url}")

		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url(redirect_url)

	except:
		frappe.log_error(title="Paynow ReturnCallback Error")
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url("/me/orders")