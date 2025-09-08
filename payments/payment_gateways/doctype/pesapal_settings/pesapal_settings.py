# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

"""
# Integrating Pesapal v3

### Validate Currency

Example:

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("Pesapal")
	controller().validate_transaction_currency(currency)

### 2. Redirect for payment

Example:

	payment_details = {
		"amount": 600,
		"title": "Payment for bill : 111",
		"description": "payment via cart",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_email": "customer@example.com",
		"payer_name": "John Doe",
		"order_id": "111",
		"currency": "KES",
		"payment_gateway": "Pesapal",
	}

	# Redirect the user to this url
	url = controller().get_payment_url(**payment_details)

### 3. On Completion of Payment

Write a method for `on_payment_authorized` in the reference doctype

Example:

	def on_payment_authorized(payment_status):
		# this method will be called when payment is complete

##### Notes:

payment_status - payment gateway will put payment status on callback.
For Pesapal payment status is Completed, Failed, etc.
"""

import json
import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_post_request, make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url, get_request_site_address, now

from payments.utils import create_payment_gateway
from payments.payment_gateways.doctype.pesapal_settings.pesapal_utils import (
	validate_ipn_request, log_ipn_request, update_payment_request_status,
	handle_payment_completion, is_duplicate_ipn, get_pesapal_settings
)


class PesapalSettings(Document):
	supported_currencies = (
		"KES", "UGX", "TZS", "RWF", "ZMW", "USD", "EUR", "GBP"
	)

	def validate(self):
		create_payment_gateway("Pesapal")
		call_hook_method("payment_gateway_enabled", gateway="Pesapal")
		if not self.flags.ignore_mandatory:
			self.validate_pesapal_credentials()

	def validate_pesapal_credentials(self):
		"""Validate Pesapal credentials by attempting to get an access token"""
		if self.consumer_key and self.consumer_secret:
			try:
				token_response = self.get_access_token()
				if not token_response.get("token"):
					frappe.throw(_("Invalid Pesapal credentials. Please check your Consumer Key and Consumer Secret."))
			except Exception as e:
				frappe.throw(_("Failed to validate Pesapal credentials: {0}").format(str(e)))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Pesapal does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_base_url(self):
		"""Get the base URL for Pesapal API based on sandbox setting"""
		if self.is_sandbox:
			return "https://cybqa.pesapal.com/pesapalv3/api"
		else:
			return "https://pay.pesapal.com/v3/api"

	def get_access_token(self):
		"""Get access token from Pesapal API"""
		url = f"{self.get_base_url()}/Auth/RequestToken"
		
		headers = {
			"Accept": "application/json",
			"Content-Type": "application/json"
		}
		
		data = {
			"consumer_key": self.consumer_key,
			"consumer_secret": self.get_password(fieldname="consumer_secret", raise_exception=False)
		}
		
		try:
			response = make_post_request(url, headers=headers, data=json.dumps(data))
			
			if response.get("status") == "200" and response.get("token"):
				return response
			else:
				frappe.log_error(f"Pesapal token request failed: {response}", "Pesapal Authentication Error")
				frappe.throw(_("Failed to get access token from Pesapal"))
				
		except Exception as e:
			frappe.log_error(f"Pesapal token request exception: {str(e)}", "Pesapal Authentication Error")
			raise

	@frappe.whitelist()
	def test_connection(self):
		"""Test connection to Pesapal API"""
		try:
			token_response = self.get_access_token()
			if token_response.get("token"):
				return {"success": True, "message": "Connection successful"}
			else:
				return {"success": False, "error": "Failed to get access token"}
		except Exception as e:
			return {"success": False, "error": str(e)}

	@frappe.whitelist()
	def register_ipn_url(self):
		"""Register IPN URL with Pesapal"""
		if not self.ipn_url:
			site_url = get_request_site_address()
			self.ipn_url = f"{site_url}/api/method/payments.payment_gateways.doctype.pesapal_settings.pesapal_settings.handle_ipn"
		
		token_response = self.get_access_token()
		token = token_response.get("token")
		
		if not token:
			frappe.throw(_("Failed to get access token for IPN registration"))
		
		url = f"{self.get_base_url()}/URLSetup/RegisterIPN"
		
		headers = {
			"Accept": "application/json",
			"Content-Type": "application/json",
			"Authorization": f"Bearer {token}"
		}
		
		data = {
			"url": self.ipn_url,
			"ipn_notification_type": self.ipn_notification_type or "POST"
		}
		
		try:
			response = make_post_request(url, headers=headers, data=json.dumps(data))
			
			if response.get("status") == "200" and response.get("ipn_id"):
				self.ipn_id = response.get("ipn_id")
				self.save()
				return {"success": True, "ipn_id": self.ipn_id}
			else:
				frappe.log_error(f"Pesapal IPN registration failed: {response}", "Pesapal IPN Registration Error")
				frappe.throw(_("Failed to register IPN URL with Pesapal"))
				
		except Exception as e:
			frappe.log_error(f"Pesapal IPN registration exception: {str(e)}", "Pesapal IPN Registration Error")
			raise

	def get_payment_url(self, **kwargs):
		"""Create payment request and return payment URL"""
		integration_request = create_request_log(kwargs, service_name="Pesapal")
		return get_url(f"./pesapal_checkout?token={integration_request.name}")

	def create_request(self, data):
		"""Process payment request"""
		self.data = frappe._dict(data)
		
		try:
			self.integration_request = frappe.get_doc("Integration Request", self.data.token)
			self.integration_request.update_status(self.data, "Queued")
			return self.submit_order_request()
			
		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"There seems to be an issue with the server's Pesapal configuration. Please try again later."
					),
				),
				"status": 401,
			}

	def submit_order_request(self):
		"""Submit order request to Pesapal API"""
		data = json.loads(self.integration_request.data)
		
		# Get access token
		token_response = self.get_access_token()
		token = token_response.get("token")
		
		if not token:
			frappe.throw(_("Failed to get access token"))
		
		# Prepare order data
		order_data = self.prepare_order_data(data)
		
		url = f"{self.get_base_url()}/Transactions/SubmitOrderRequest"
		
		headers = {
			"Accept": "application/json",
			"Content-Type": "application/json",
			"Authorization": f"Bearer {token}"
		}
		
		try:
			response = make_post_request(url, headers=headers, data=json.dumps(order_data))
			
			if response.get("status") == "200" and response.get("redirect_url"):
				# Update integration request with order tracking ID
				self.integration_request.update_status({
					"order_tracking_id": response.get("order_tracking_id"),
					"merchant_reference": response.get("merchant_reference")
				}, "Initiated")
				
				return {
					"redirect_to": response.get("redirect_url"),
					"status": 200
				}
			else:
				frappe.log_error(f"Pesapal order submission failed: {response}", "Pesapal Order Submission Error")
				self.integration_request.update_status(data, "Failed")
				return {
					"redirect_to": frappe.redirect_to_message(
						_("Payment Error"),
						_("Failed to create payment request. Please try again.")
					),
					"status": 400
				}
				
		except Exception as e:
			frappe.log_error(f"Pesapal order submission exception: {str(e)}", "Pesapal Order Submission Error")
			self.integration_request.update_status(data, "Failed")
			raise

	def prepare_order_data(self, data):
		"""Prepare order data for Pesapal API"""
		# Ensure we have IPN ID
		if not self.ipn_id:
			frappe.throw(_("IPN URL not registered. Please register IPN URL first."))
		
		# Get callback URL
		callback_url = data.get("redirect_to") or get_url("payment-success")
		if "?" in callback_url:
			callback_url += "&"
		else:
			callback_url += "?"
		callback_url += urlencode({
			"reference_doctype": data.get("reference_doctype"),
			"reference_docname": data.get("reference_docname")
		})
		
		order_data = {
			"id": data.get("order_id") or data.get("reference_docname"),
			"currency": data.get("currency"),
			"amount": float(data.get("amount")),
			"description": data.get("description") or data.get("title"),
			"callback_url": callback_url,
			"notification_id": self.ipn_id,
			"billing_address": {
				"email_address": data.get("payer_email"),
				"phone_number": data.get("payer_phone"),
				"first_name": data.get("payer_name", "").split(" ")[0] if data.get("payer_name") else "",
				"last_name": " ".join(data.get("payer_name", "").split(" ")[1:]) if data.get("payer_name") else "",
				"country_code": "KE",  # Default to Kenya, can be made configurable
			}
		}
		
		# Add optional fields if available
		if data.get("cancellation_url"):
			order_data["cancellation_url"] = data.get("cancellation_url")
		
		return order_data

	def get_transaction_status(self, order_tracking_id):
		"""Get transaction status from Pesapal API"""
		token_response = self.get_access_token()
		token = token_response.get("token")

		if not token:
			frappe.throw(_("Failed to get access token"))

		url = f"{self.get_base_url()}/Transactions/GetTransactionStatus"

		headers = {
			"Accept": "application/json",
			"Authorization": f"Bearer {token}"
		}

		params = {"orderTrackingId": order_tracking_id}

		try:
			response = make_get_request(url, headers=headers, params=params)
			return response
		except Exception as e:
			frappe.log_error(f"Pesapal transaction status check failed: {str(e)}", "Pesapal Transaction Status Error")
			raise

	def process_payment_callback(self, order_tracking_id, merchant_reference):
		"""Process payment callback from Pesapal"""
		try:
			# Get transaction status
			status_response = self.get_transaction_status(order_tracking_id)

			if not status_response:
				frappe.log_error("No response from Pesapal transaction status API", "Pesapal Callback Error")
				return False

			# Find the integration request
			integration_requests = frappe.get_all(
				"Integration Request",
				filters={"data": ["like", f"%{merchant_reference}%"]},
				limit=1
			)

			if not integration_requests:
				frappe.log_error(f"Integration request not found for merchant reference: {merchant_reference}", "Pesapal Callback Error")
				return False

			integration_request = frappe.get_doc("Integration Request", integration_requests[0].name)

			# Update integration request status using utility function
			new_status = update_payment_request_status(integration_request, status_response)
			self.flags.status_changed_to = new_status

			# Handle payment completion using utility function
			success = handle_payment_completion(integration_request, status_response, new_status)

			if success:
				frappe.logger().info(f"Successfully processed payment callback for order {order_tracking_id}")
			else:
				frappe.log_error(f"Failed to complete payment processing for order {order_tracking_id}", "Pesapal Payment Processing Error")

			return success

		except Exception as e:
			frappe.log_error(f"Error processing Pesapal callback: {str(e)}", "Pesapal Callback Error")
			return False

	@frappe.whitelist()
	def clear(self):
		"""Clear Pesapal settings"""
		self.consumer_key = self.consumer_secret = None
		self.ipn_url = self.ipn_id = None
		self.flags.ignore_mandatory = True
		self.save()


@frappe.whitelist(allow_guest=True)
def handle_ipn():
	"""Handle IPN (Instant Payment Notification) from Pesapal"""
	ipn_log_id = None

	try:
		# Get IPN data
		if frappe.request.method == "POST":
			ipn_data = frappe.local.form_dict
		else:
			ipn_data = frappe.local.form_dict

		# Log the IPN request
		ipn_log_id = log_ipn_request(ipn_data, "Processing")

		# Validate IPN request
		if not validate_ipn_request(ipn_data):
			frappe.local.response.update({
				"status": 400,
				"message": "Invalid IPN data"
			})
			if ipn_log_id:
				frappe.get_doc("Integration Request", ipn_log_id).update_status(ipn_data, "Failed")
			return

		order_tracking_id = ipn_data.get("OrderTrackingId")
		merchant_reference = ipn_data.get("OrderMerchantReference")
		notification_type = ipn_data.get("OrderNotificationType")

		# Check for duplicate IPN
		if is_duplicate_ipn(order_tracking_id, merchant_reference):
			frappe.logger().info(f"Duplicate IPN received for order {order_tracking_id}")
			frappe.local.response.update({
				"orderNotificationType": notification_type,
				"orderTrackingId": order_tracking_id,
				"orderMerchantReference": merchant_reference,
				"status": 200
			})
			if ipn_log_id:
				frappe.get_doc("Integration Request", ipn_log_id).update_status(ipn_data, "Completed")
			return

		if notification_type == "IPNCHANGE" and order_tracking_id and merchant_reference:
			# Get Pesapal settings
			pesapal_settings = get_pesapal_settings()

			# Process the payment callback
			pesapal_settings.process_payment_callback(order_tracking_id, merchant_reference)

			# Respond to Pesapal
			frappe.local.response.update({
				"orderNotificationType": "IPNCHANGE",
				"orderTrackingId": order_tracking_id,
				"orderMerchantReference": merchant_reference,
				"status": 200
			})

			# Update IPN log
			if ipn_log_id:
				frappe.get_doc("Integration Request", ipn_log_id).update_status(ipn_data, "Completed")
		else:
			frappe.local.response.update({
				"status": 400,
				"message": "Invalid IPN data"
			})
			if ipn_log_id:
				frappe.get_doc("Integration Request", ipn_log_id).update_status(ipn_data, "Failed")

	except Exception as e:
		frappe.log_error(f"IPN handling error: {str(e)}", "Pesapal IPN Error")
		frappe.local.response.update({
			"status": 500,
			"message": "Internal server error"
		})
		if ipn_log_id:
			try:
				frappe.get_doc("Integration Request", ipn_log_id).update_status({"error": str(e)}, "Failed")
			except:
				pass


@frappe.whitelist(allow_guest=True)
def handle_callback():
	"""Handle payment callback from Pesapal"""
	try:
		order_tracking_id = frappe.form_dict.get("OrderTrackingId")
		merchant_reference = frappe.form_dict.get("OrderMerchantReference")
		notification_type = frappe.form_dict.get("OrderNotificationType")

		if notification_type == "CALLBACKURL" and order_tracking_id and merchant_reference:
			# Get Pesapal settings and process callback
			pesapal_settings = frappe.get_doc("Pesapal Settings")
			pesapal_settings.process_payment_callback(order_tracking_id, merchant_reference)

			# Redirect to appropriate page
			redirect_to = frappe.form_dict.get("redirect_to")
			if redirect_to:
				frappe.local.flags.redirect_location = redirect_to
			else:
				reference_doctype = frappe.form_dict.get("reference_doctype")
				reference_docname = frappe.form_dict.get("reference_docname")
				if reference_doctype and reference_docname:
					frappe.local.flags.redirect_location = f"/payment-success?doctype={reference_doctype}&docname={reference_docname}"
				else:
					frappe.local.flags.redirect_location = "/payment-success"
		else:
			frappe.local.flags.redirect_location = "/payment-failed"

	except Exception as e:
		frappe.log_error(f"Callback handling error: {str(e)}", "Pesapal Callback Error")
		frappe.local.flags.redirect_location = "/payment-failed"
