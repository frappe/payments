import frappe
import json
from frappe import _
from string import Template
from Crypto.Cipher import AES
from frappe.model.document import Document
from payments.utils import create_payment_gateway
from frappe.utils import call_hook_method, get_url
from frappe.integrations.utils import create_request_log
from urllib.parse import parse_qsl

class BankMuscatSettings(Document):

	supported_currencies = ("OMR", "AED", "USD", "GBP", "EUR", "INR")

	# Validate and create a Payment Gateway entry automatically when BankMuscat Settings is saved
	def on_update(self):
		try:
			if not self.merchant_id:
				frappe.throw(_("Merchant ID is required to create a Payment Gateway."))

			gateway_name = f"BankMuscat-{self.merchant_id}"
			if not frappe.db.exists("Payment Gateway", gateway_name):
				create_payment_gateway(
					gateway_name,
					settings="BankMuscat Settings",
					controller=self.merchant_id,
				)
				frappe.logger().info(f"[BankMuscat] Created new Payment Gateway: {gateway_name}")
			else:
				frappe.logger().info(f"[BankMuscat] Payment Gateway already exists: {gateway_name}")

			call_hook_method("payment_gateway_enabled", gateway=gateway_name)
		except Exception:
			error_trace = frappe.get_traceback()
			frappe.log_error(
				message=error_trace,
				title=f"BankMuscat Payment Gateway Creation Failed (Merchant: {self.merchant_id or 'Unknown'})"
			)

			frappe.throw(
				_(
					"Unable to create or update Payment Gateway for Merchant ID {0}. Please check the Error Log for details."
				).format(self.merchant_id or _("Unknown")),
				title=_("Payment Gateway Error"),
			)

	# Validate input and create an Integration Request entry to generate and return the payment URL via email
	def get_payment_url(self, **kwargs):
		try:
			if not kwargs or not isinstance(kwargs, dict):
				frappe.throw("Missing or invalid parameters for BankMuscat payment request.")

			required_fields = ["amount", "reference_doctype", "reference_docname", "currency", "payment_gateway"]
			missing_fields = [field for field in required_fields if field not in kwargs or not kwargs[field]]
			if missing_fields:
				frappe.throw(f"Missing required fields: {', '.join(missing_fields)}")

			self.order_id = create_request_log(
				data=kwargs,
				service_name="BankMuscat",
				name=kwargs.get("order_id", "")
			).name

			return get_url(f"bankmuscat_checkout?order_id={self.order_id}")
		except Exception:
			error_message = frappe.get_traceback()
			frappe.log_error(error_message, "BankMuscat get_payment_url Failed")
			if getattr(self, "order_id", None):
				order_name = getattr(self.order_id, "name", self.order_id)
				frappe.db.set_value(
					"Integration Request",
					order_name,
					"error",
					error_message[:1000]
				)
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
			encrypted_hex = (cipher.nonce + ciphertext + tag).hex()
			return encrypted_hex
		except Exception:
			frappe.log_error(frappe.get_traceback(), "BankMuscat Encryption Failed")
			frappe.throw(_("Unable to encrypt request for BankMuscat. Please check the Error Log."))	

	# Prepare and return the merchant data string required by BankMuscat gateway.
	def get_merchant_data(self, **kwargs):
		try:
			# Base URL for redirect and cancel URLs
			base_url = get_url("api/method/payments.templates.pages.bankmuscat_checkout")

			if not base_url:
				frappe.throw(_("Unable to generate redirect base URL for BankMuscat checkout."))

			order_id = (kwargs.get("order_id") or str(getattr(self, "order_id", ""))).replace("-", "")
			if not order_id:
				frappe.throw(_("Missing 'order_id' in payment data."))

			amount = kwargs.get("amount")
			if not amount:
				frappe.throw(_("Missing 'amount' for BankMuscat transaction."))

			currency = kwargs.get("currency") or "OMR"
			if not currency:
				frappe.throw(_("Missing 'currency' in payment data."))

			merchant_data = {
				"merchant_id": kwargs.get("merchant_id", str(self.merchant_id)),
				"order_id": order_id,
				"currency": currency,
				"amount": str(amount),
				"redirect_url": f"{base_url}.verify_payment_status",
				"cancel_url": f"{base_url}.cancel_payment",
				"integration_type": kwargs.get("integration_type", "iframe_normal"),
			}

			# add optional fields
			optional_fields = [
				"language",
				"billing_name","billing_address","billing_city","billing_state",
				"billing_zip","billing_country","billing_tel","billing_email",
				"delivery_name","delivery_address","delivery_city","delivery_state",
				"delivery_zip","delivery_country","delivery_tel",
				"merchant_param1","merchant_param2","merchant_param3","merchant_param4","merchant_param5",
				"promo_code","customer_identifier",
			]

			merchant_data.update({field: kwargs.get(field, "") for field in optional_fields})

			return "&".join(f"{key}={value}" for key, value in merchant_data.items())
		except Exception as e:
			frappe.log_error(frappe.get_traceback(), "BankMuscat: get_merchant_data Failed")
			frappe.throw(_("Unable to prepare merchant data for BankMuscat. Please check the Error Log."))	

	# Validate that the provided currency is supported by BankMuscat.
	def validate_transaction_currency(self, currency):
		if not currency:
			frappe.throw(_("Currency is missing"))
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"BankMuscat does not support transactions in currency {0}. Please select another payment method."
				).format(currency)
			)

	# Validate all mandatory fields required for processing a BankMuscat transaction
	def validate_mandatory_values(self, **kwargs):
		self.validate_transaction_currency(kwargs.get("currency"))

		amount = kwargs.get("amount")
		if not amount:
			frappe.throw(_("Amount is missing"))
		elif float(amount) <= 0:
			frappe.throw(_("Amount must be greater than zero"))

		order_id = kwargs.get("order_id") or getattr(self, "order_id", None)
		if not order_id:
			frappe.throw(_("Parameter 'order_id' is missing"))	

	# Generate the BankMuscat payment page URL and return an auto-submitting HTML form.
	def get_payment_page_url(self, log, **kwargs):
		try:
			self.validate_mandatory_values(**kwargs)

			merchant_data = self.get_merchant_data(**kwargs)

			working_key = self.get_password("working_key")

			if not working_key:
				frappe.throw(_("Missing 'working_key' in BankMuscat Settings."))

			encrypted_req = self.encrypt(merchant_data, working_key)

			frappe.logger().info(f"[BankMuscat] Encrypted request generated for Order ID: {kwargs.get('order_id')}")

			xscode = self.get_password("access_code")
	
			base_url = self.base_url

			if not base_url:
				frappe.throw(_("Base URL is not configured in BankMuscat Settings."))

			if not xscode:
				frappe.throw(_("Access code is missing in BankMuscat Settings."))

			action_url = f"{base_url}/transaction.do?command=initiateTransaction"

			html = Template(
				"""
				<form id="nonseamless" method="POST" name="redirect" action="$action_url">
					<input type="hidden" id="encRequest" name="encRequest" value="$encReq">
					<input type="hidden" name="access_code" id="access_code" value="$xscode">
					<script language="javascript">document.redirect.submit();</script>
				</form>
				"""
				).safe_substitute(
					encReq=encrypted_req, 
					xscode=xscode, 
					action_url=action_url
				)

			log.payload_before_encryption = encrypted_req
			log.payment_url = action_url
			log.request_data = json.dumps(merchant_data, indent=2)
			log.save(ignore_permissions=True)
				
			return html
		except Exception:
			frappe.log_error(frappe.get_traceback(), "BankMuscat get_gateway_url Failed")
			frappe.throw(
				_("Unable to generate BankMuscat payment form. Please check the Error Log for details.")
			)		

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
			return

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
