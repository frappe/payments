from string import Template

import frappe
from Crypto.Cipher import AES
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payments.utils import create_payment_gateway


class BankMuscatSettings(Document):
	supported_currencies = ("INR", "OMR", "AED", "USD", "GBP", "EUR")

	def on_update(self):
		create_payment_gateway(
			f"BankMuscat-{self.merchant_id}",
			settings="BankMuscat Settings",
			controller=self.merchant_id,
		)
		call_hook_method("payment_gateway_enabled", gateway=f"BankMuscat-{self.merchant_id}")

	def get_payment_url(self, **kwargs):
		frappe.log_error("data: ", kwargs)
		self.order_id = create_request_log(
			kwargs, service_name="BankMuscat", name=kwargs.get("order_id", "")
		).name
		return get_url(f"bankmuscat_checkout?order_id={self.order_id}")

	def decrypt(self, cipher_text, working_key):
		cipher_text = bytes.fromhex(cipher_text)
		nonce, ciphertext, tag = cipher_text[: AES.block_size], cipher_text[16:-16], cipher_text[-16:]
		cipher = AES.new(working_key.encode(), AES.MODE_GCM, nonce=nonce)
		return cipher.decrypt_and_verify(ciphertext, tag)

	def encrypt(self, plain_text, working_key):
		cipher = AES.new(working_key.encode(), AES.MODE_GCM)
		ciphertext, tag = cipher.encrypt_and_digest(plain_text.encode())
		return (cipher.nonce + ciphertext + tag).hex()

	def get_merchant_data(self, **kwargs):
		base_url = get_url("api/method/payments.templates.pages.bankmuscat_checkout")

		merchant_data = {
			"merchant_id": kwargs.get("merchant_id", str(self.merchant_id)),
			"order_id": (kwargs.get("order_id") or str(self.order_id)).replace("-", ""),
			# "currency": kwargs.get("currency") or "OMR",
			"currency": "OMR",
			"amount": str(kwargs.get("amount", "")),
			"redirect_url": f"{base_url}.verify_payment_status",
			"cancel_url": f"{base_url}.cancel_payment",
			"integration_type": kwargs.get("integration_type", "iframe_normal"),
		}
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

	def get_encrypted_request(self, **kwargs):
		return self.encrypt(self.get_merchant_data(**kwargs), self.get_password("working_key"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				f"Please select another payment method. BankMuscat does not support transactions in currency '{currency}'"
			)

	def validate_mandatory_values(self, **kwargs):
		self.validate_transaction_currency(kwargs.get("currency"))
		if not kwargs.get("amount"):
			frappe.throw("Amount is missing")
		if not kwargs.get("order_id") and not getattr(self, "order_id", None):
			frappe.throw("Param order_id is missing")

	def get_gateway_url(self, **kwargs):
		self.validate_mandatory_values(**kwargs)
		encrypted_req = self.get_encrypted_request(**kwargs)
		xscode = self.get_password("access_code")
		custom_uat = self.custom_uat

		action_url = (
			"https://spayuattrns.bmtest.om/transaction.do?command=initiateTransaction"
			if custom_uat == "Staging"
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

	def get_payment_page_url(self, **kwargs):
		return self.get_gateway_url(**kwargs)


def get_gateway_controller(doctype, docname, payment_gateway=None):
	if not payment_gateway:
		reference_doc = frappe.get_doc(doctype, docname)
		payment_gateway = reference_doc.payment_gateway
	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")


def store_custom_name(doc, method=None):
	doc.db_set("custom_name", doc.name.replace("-", ""))
