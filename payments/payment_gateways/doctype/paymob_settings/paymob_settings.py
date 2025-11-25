# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt

from datetime import timedelta
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.model.document import Document
from frappe.utils import get_datetime, now_datetime

from payments.payment_gateways.paymob.accept_api import AcceptAPI
from payments.payment_gateways.paymob.hmac_validator import HMACValidator
from payments.payment_gateways.paymob.paymob_urls import PaymobUrls
from payments.payment_gateways.paymob.response_codes import SUCCESS


class PaymobSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password
		hmac: DF.Password
		iframe: DF.Data
		payment_integration: DF.Int
		public_key: DF.Password
		secret_key: DF.Password
		token: DF.Password | None
	# end: auto-generated types

	@frappe.whitelist()
	def refresh_access_token(self):
		"""
		If existing token expired → fetch new one
		"""

		accept = AcceptAPI()
		token = accept.retrieve_auth_token()
		self.token = token
		self.expires_in = now_datetime() + timedelta(minutes=50)
		self.save(ignore_permissions=True)

		return token

	def get_valid_token(self):
		token = self.get_password("token") if self.token else None

		buffer = timedelta(minutes=2)
		if token and self.expires_in:
			expires_in = (
				get_datetime(self.expires_in) if isinstance(self.expires_in, str) else self.expires_in
			)
			if now_datetime() + buffer < expires_in:
				return token

		return self.refresh_access_token()

	def get_payment_url(self, **kwargs):
		try:
			paymob_urls = PaymobUrls()

			if not kwargs.get("order_id") or not kwargs.get("amount"):
				frappe.throw(_("Missing order ID or amount"))

			# Build dummy billing data
			billing_data = {
				"apartment": "NA",
				"email": kwargs.get("payer_email"),
				"floor": "NA",
				"first_name": kwargs.get("payer_name").split()[0],
				"street": "NA",
				"building": "NA",
				"phone_number": "+201111111111",
				"shipping_method": "NA",
				"postal_code": "NA",
				"city": "Cairo",
				"country": "EG",
				"last_name": kwargs.get("payer_name").split()[-1],
				"state": "NA",
			}

			payment_key_payload = {
				"auth_token": self.get_valid_token(),
				"amount_cents": str(int(float(kwargs.get("amount")) * 100)),
				"expiration": 3600,
				"order_id": kwargs.get("order_id"),
				"currency": kwargs.get("currency", "EGP"),
				"billing_data": billing_data,
				"integration_id": self.payment_integration,
			}

			url = paymob_urls.get_url("payment_key")
			headers = {"Content-Type": "application/json"}
			response = make_post_request(url=url, json=payment_key_payload, headers=headers)

			if not response or "token" not in response:
				frappe.throw(_("Failed to retrieve payment token from Paymob"))

			payment_token = response["token"]

			iframe_url = f"https://accept.paymob.com/api/acceptance/iframes/{self.iframe}?payment_token={payment_token}"
			return iframe_url

		except Exception:
			frappe.log_error(frappe.get_traceback())
			frappe.throw(_("Could not generate Paymob payment URL"))

	def create_order(self, **kwargs):
		integration_request = create_request_log(kwargs, service_name="Paymob")
		paymob_urls = PaymobUrls()

		token = self.get_valid_token()

		amount_cents = int(kwargs.get("amount")) * 100  # Paymob uses cents
		currency = kwargs.get("currency", "EGP")
		delivery_needed = kwargs.get("delivery_needed", False)
		items = kwargs.get("items", [])

		payload = {
			"auth_token": token,
			"delivery_needed": str(delivery_needed).lower(),
			"amount_cents": str(amount_cents),
			"currency": currency,
			"items": items,
		}

		try:
			url = paymob_urls.get_url("order")
			headers = {"Content-Type": "application/json"}
			order = make_post_request(url=url, json=payload, headers=headers)

			if not order or not order.get("id"):
				frappe.throw(_("Failed to create order in Paymob"))

			paymob_order_id = order.get("id")

			integration_request_dict = frappe.parse_json(integration_request.data)
			integration_request_dict["paymob_order_id"] = str(paymob_order_id)

			order["integration_request"] = integration_request.name

			integration_request.data = frappe.as_json(integration_request_dict)
			integration_request.save(ignore_permissions=True)
			frappe.db.commit()

			return order
		except Exception:
			frappe.log_error(frappe.get_traceback())
			frappe.throw(_("Could not create Paymob order"))


@frappe.whitelist(allow_guest=True)
def callback():
	try:
		incoming_hmac = frappe.request.args.get("hmac") or frappe.request.form.get("hmac")

		if not incoming_hmac:
			frappe.throw(_("Missing HMAC"))

		incoming_data_json = frappe.request.get_json()

		# Validate the HMAC
		validator = HMACValidator(incoming_hmac=incoming_hmac, callback_dict=incoming_data_json)

		if not validator.is_valid:
			frappe.throw(_("Invalid HMAC"))

		obj_data = incoming_data_json.get("obj", {})
		success = obj_data.get("success")
		pending = obj_data.get("pending")
		payment_status = obj_data.get("order", {}).get("payment_status")
		txn_response_code = obj_data.get("data", {}).get("txn_response_code")
		migs_data = obj_data.get("data", {}).get("migs_order", {})
		capture_status = migs_data.get("status")
		paymob_payment_id = obj_data.get("id")
		paymob_order_id = obj_data.get("order", {}).get("id")

		is_payment_successful = (
			success is True
			and pending is False
			and str(payment_status).upper() == "PAID"
			and str(txn_response_code).upper() == "APPROVED"
		)

		if not paymob_order_id:
			frappe.throw(_("Missing order ID"))

		integration_request_doc = get_integration_request(paymob_order_id)
		integration_request_dict = frappe.parse_json(integration_request_doc.data)

		integration_request_dict.update(
			{
				"paymob_payment_id": str(paymob_payment_id),
				"order_id": str(paymob_order_id),
			}
		)

		integration_request_doc.data = frappe.as_json(integration_request_dict)

		if is_payment_successful:
			if capture_status == "CAPTURED":
				integration_request_doc.status = "Completed"

			integration_request_doc.save(ignore_permissions=True)
			frappe.db.commit()

			handle_payment_success(integration_request_dict)

		else:
			integration_request_doc.error = (
				f"Payment Status: {payment_status}, Response Code: {txn_response_code}"
			)
			integration_request_doc.save(ignore_permissions=True)
			frappe.db.commit()
			frappe.log_error(frappe.get_traceback(), "Paymob Payment not authorized")

	except Exception:
		frappe.log_error(frappe.get_traceback(), "Paymob Callback Error")


def get_integration_request(paymob_order_id):
	"""Fetch Integration Request linked to Paymob order."""

	integration_requests = frappe.get_all(
		"Integration Request",
		filters={
			"integration_request_service": "Paymob",
			"data": ["like", f'%"paymob_order_id": "{paymob_order_id}"%'],
		},
		fields=["name", "data", "reference_doctype", "reference_docname"],
		order_by="creation desc",
		limit=1,
	)
	if not integration_requests:
		frappe.throw(_("No Integration Request found for this order"))

	return frappe.get_doc("Integration Request", integration_requests[0].name)


def handle_payment_success(integration_request_dict):
	"""Handle post-success payments"""

	redirect_to = integration_request_dict["redirect_to"]
	if integration_request_dict["reference_doctype"] and integration_request_dict["reference_docname"]:
		custom_redirect_to = None
		try:
			custom_redirect_to = frappe.get_doc(
				integration_request_dict["reference_doctype"], integration_request_dict["reference_docname"]
			).run_method("on_payment_authorized", "Completed")

		except Exception:
			frappe.log_error(frappe.get_traceback())

		if custom_redirect_to:
			redirect_to = custom_redirect_to

	redirect_url = f"payment-success?doctype={integration_request_dict['reference_doctype']}&docname={integration_request_dict['reference_docname']}"

	if redirect_to:
		redirect_url += "&" + urlencode({"redirect_to": redirect_to})

	return {"redirect_to": redirect_url, "status": "Completed"}


@frappe.whitelist()
def update_paymob_settings(**kwargs):
	args = frappe._dict(kwargs)
	fields = frappe._dict(
		{
			"api_key": args.get("api_key"),
			"secret_key": args.get("secret_key"),
			"public_key": args.get("public_key"),
			"hmac": args.get("hmac"),
			"iframe": args.get("iframe"),
			"payment_integration": args.get("payment_integration"),
		}
	)
	try:
		paymob_settings = frappe.get_doc("Paymob Settings").update(fields)
		paymob_settings.save()
		return "Paymob Credentials Successfully"
	except Exception:
		return "Failed to Update Paymob Credentials"
