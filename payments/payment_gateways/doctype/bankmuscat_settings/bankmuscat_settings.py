from string import Template

import frappe
from Crypto.Cipher import AES
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import create_payment_gateway


class BankMuscatSettings(Document):
	supported_currencies = ("OMR", "AED", "USD", "GBP", "EUR")

	# Create Payment Gateway on save
	def on_update(self):
		try:
			gateway_name = f"BankMuscat-{self.merchant_id}"

			# Check if Payment Gateway already exists
			if not frappe.db.exists("Payment Gateway", gateway_name):
				create_payment_gateway(
					gateway_name,
					settings="BankMuscat Settings",
					controller=self.merchant_id,
				)
				frappe.logger().info(f"Created new Payment Gateway: {gateway_name}")
			else:
				frappe.logger().info(f"Payment Gateway already exists: {gateway_name}")

			# Trigger hook
			call_hook_method("payment_gateway_enabled", gateway=gateway_name)

		except Exception:
			frappe.log_error(
				message=frappe.get_traceback(),
				title=f"BankMuscat Gateway Creation Failed (Merchant: {self.merchant_id})",
			)
			frappe.throw(
				_(
					"Unable to create or update Payment Gateway for Merchant ID {0}. Please check the Error Log."
				).format(self.merchant_id),
				title=_("Payment Gateway Error"),
			)

	def get_payment_url(self, **kwargs):
		try:
			frappe.logger().info(f"[BankMuscat] get_payment_url args: {kwargs}")
			self.order_id = create_request_log(
				kwargs, service_name="BankMuscat", name=kwargs.get("order_id", "")
			).name
			return get_url(f"bankmuscat_checkout?order_id={self.order_id}")
		except Exception:
			frappe.log_error(frappe.get_traceback(), "BankMuscat get_payment_url Failed")
			frappe.throw("Unable to generate payment URL. Please check the Error Log.")

	def decrypt(self, cipher_text, working_key):
		try:
			cipher_text = bytes.fromhex(cipher_text)
			nonce, ciphertext, tag = cipher_text[: AES.block_size], cipher_text[16:-16], cipher_text[-16:]
			cipher = AES.new(working_key.encode(), AES.MODE_GCM, nonce=nonce)
			return cipher.decrypt_and_verify(ciphertext, tag)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "BankMuscat Decryption Failed")
			frappe.throw("Unable to decrypt response from BankMuscat.")

	def encrypt(self, plain_text, working_key):
		try:
			cipher = AES.new(working_key.encode(), AES.MODE_GCM)
			ciphertext, tag = cipher.encrypt_and_digest(plain_text.encode())
			return (cipher.nonce + ciphertext + tag).hex()
		except Exception:
			frappe.log_error(frappe.get_traceback(), "BankMuscat Encryption Failed")
			frappe.throw("Unable to encrypt request for BankMuscat.")

	def get_merchant_data(self, **kwargs):
		# Base URL for redirect and cancel URLs
		base_url = get_url("api/method/payments.templates.pages.bankmuscat_checkout")

		# mandatory fields
		merchant_data = {
			"merchant_id": kwargs.get("merchant_id", str(self.merchant_id)),
			"order_id": (kwargs.get("order_id") or str(self.order_id)).replace("-", ""),
			"currency": kwargs.get("currency") or "OMR",
			"amount": str(kwargs.get("amount", "")),
			"redirect_url": f"{base_url}.verify_payment_status",
			"cancel_url": f"{base_url}.cancel_payment",
			"integration_type": kwargs.get("integration_type", "iframe_normal"),
		}

		# add optional fields
		optional_fields = [
			"language",
			"billing_name",
			"billing_address",
			"billing_city",
			"billing_state",
			"billing_zip",
			"billing_country",
			"billing_tel",
			"billing_email",
			"delivery_name",
			"delivery_address",
			"delivery_city",
			"delivery_state",
			"delivery_zip",
			"delivery_country",
			"delivery_tel",
			"merchant_param1",
			"merchant_param2",
			"merchant_param3",
			"merchant_param4",
			"merchant_param5",
			"promo_code",
			"customer_identifier",
		]

		merchant_data.update({field: kwargs.get(field, "") for field in optional_fields})

		return "&".join(f"{key}={value}" for key, value in merchant_data.items())

	def validate_transaction_currency(self, currency):
		if not currency:
			frappe.throw(_("Currency is missing"))
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"BankMuscat does not support transactions in currency {0}. Please select another payment method."
				).format(currency)
			)

	# validate mandatory values
	def validate_mandatory_values(self, **kwargs):
		self.validate_transaction_currency(kwargs.get("currency"))

		if not kwargs.get("amount"):
			frappe.throw("Amount is missing")

		if not kwargs.get("order_id") and not getattr(self, "order_id", None):
			frappe.throw("Param order_id is missing")

	# Gateway url
	def get_payment_page_url(self, **kwargs):
		try:
			self.validate_mandatory_values(**kwargs)
			encrypted_req = self.encrypt(self.get_merchant_data(**kwargs), self.get_password("working_key"))
			xscode = self.get_password("access_code")

			action_url = (
				"https://spayuattrns.bmtest.om/transaction.do?command=initiateTransaction"
				if self.custom_uat == "Staging"
				else "https://smartpaytrns.bankmuscat.com/transaction.do?command=initiateTransaction"
			)

			html = Template(
				"""<form id="nonseamless" method="POST" name="redirect" action="$action_url">
					<input type="hidden" id="encRequest" name="encRequest" value="$encReq">
					<input type="hidden" name="access_code" id="access_code" value="$xscode">
					<script language="javascript">document.redirect.submit();</script>
				</form>"""
			).safe_substitute(encReq=encrypted_req, xscode=xscode, action_url=action_url)

			return html
		except Exception:
			frappe.log_error(frappe.get_traceback(), "BankMuscat get_gateway_url Failed")
			frappe.throw("Unable to generate BankMuscat payment form. Please check the Error Log.")


# Gateway controller resolver
def get_gateway_controller(doctype, docname, payment_gateway=None):
	if not payment_gateway:
		reference_doc = frappe.get_doc(doctype, docname)
		payment_gateway = reference_doc.payment_gateway

	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")


# store custom name without hyphen if Payment Gateway is BankMuscat
def store_custom_name(doc, method=None):
	try:
		if "BankMuscat" not in (doc.payment_gateway_account or ""):
			return  # skip silently

		if not hasattr(doc, "name"):
			frappe.throw(_("Document has no name attribute to generate custom_name."))

		clean_name = doc.name.replace("-", "")
		doc.db_set("custom_name", clean_name)

	except Exception:
		frappe.log_error(
			message=frappe.get_traceback(),
			title=f"Failed to set custom_name for Payment Request {getattr(doc, 'name', 'Unknown')}",
		)
		frappe.throw(
			_("Unable to store custom name for Payment Request {0}. Please check the Error Log.").format(
				getattr(doc, "name", "Unknown")
			),
			title=_("Custom Name Error"),
		)
