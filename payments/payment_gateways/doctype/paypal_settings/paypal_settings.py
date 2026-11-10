# Copyright (c) 2015, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""
# Integrating PayPal

With the Client ID and Client Secret of a PayPal app, payments are taken with PayPal's REST API
(Orders v2). Recurring payments, and sites with only the classic API Username, Password and
Signature, use PayPal's classic NVP API.

### 1. Validate Currency Support

Example:

	from payments.utils import get_payment_gateway_controller

	controller = get_payment_gateway_controller("PayPal")
	controller.validate_transaction_currency(currency)

### 2. Redirect for payment

Example:

	payment_details = {
		"amount": 600,
		"title": "Payment for bill : 111",
		"description": "payment via cart",
		"reference_doctype": "Payment Request",
		"reference_docname": "PR0001",
		"payer_email": "NuranVerkleij@example.com",
		"payer_name": "Nuran Verkleij",
		"order_id": "111",
		"currency": "USD",
		"payment_gateway": "PayPal",
		"subscription_details": {  # classic API only
			"plan_id": "plan_12313", # if Required
			"start_date": "2018-08-30",
			"billing_period": "Month" #(Day, Week, SemiMonth, Month, Year),
			"billing_frequency": 1,
			"customer_notify": 1,
			"upfront_amount": 1000
		}
	}

	# redirect the user to this url
	url = controller.get_payment_url(**payment_details)

A Payment Request is paid what it still asks, in its currency.

### 3. On Completion of Payment

Write a method for `on_payment_authorized` in the reference doctype

Example:

	def on_payment_authorized(payment_status):
		# your code to handle callback

##### Note:

payment_status - payment gateway will put payment status on callback.
For paypal payment status parameter is one from: [Completed, Cancelled, Failed]

### 4. Refunds

	controller.refund("Payment Request", "PR0001", 25, "ACC-SINV-RET-0001")

refunds part or all of a payment taken with the REST API, once for the given key (e.g. the credit
note it is for), and returns PayPal's refund id.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log, make_post_request
from frappe.model.document import Document
from frappe.rate_limiter import rate_limit
from frappe.utils import (
	call_hook_method,
	cint,
	convert_utc_to_system_timezone,
	cstr,
	escape_html,
	flt,
	fmt_money,
	format_datetime,
	get_datetime,
	get_url,
	validate_email_address,
)
from frappe.utils.data import get_system_timezone

from payments.utils import create_payment_gateway
from payments.utils.payment_request import NothingToPay, amount_to_pay, first_time, tell_staff

api_path = "/api/method/payments.payment_gateways.doctype.paypal_settings.paypal_settings"

SANDBOX_HOST = "https://api-m.sandbox.paypal.com"
LIVE_HOST = "https://api-m.paypal.com"

# currencies PayPal takes without decimals
ZERO_DECIMAL_CURRENCIES = frozenset(("HUF", "JPY", "TWD"))

# Integration Requests of an order a payer pays, and of a refund of one
ORDER = "PayPal Order"
REFUND = "PayPal Refund"

SIGNATURE_HEADERS = {
	"auth_algo": "PAYPAL-AUTH-ALGO",
	"cert_url": "PAYPAL-CERT-URL",
	"transmission_id": "PAYPAL-TRANSMISSION-ID",
	"transmission_sig": "PAYPAL-TRANSMISSION-SIG",
	"transmission_time": "PAYPAL-TRANSMISSION-TIME",
}


class PayPalError(frappe.ValidationError):
	"""An error PayPal's REST API answered with."""

	def __init__(self, status_code: int, body: dict | None = None):
		body = body if isinstance(body, dict) else {}
		detail = (body.get("details") or [{}])[0]
		self.status_code = status_code
		self.issue = detail.get("issue") or body.get("name") or body.get("error")
		self.debug_id = body.get("debug_id")
		message = detail.get("description") or body.get("message") or body.get("error_description") or ""
		super().__init__(
			_("PayPal refused the request ({0}): {1}").format(self.issue or status_code, message)
			+ (f" (debug_id {self.debug_id})" if self.debug_id else "")
		)


NotPayable = NothingToPay


class Message(NamedTuple):
	"""A message page to send the payer to."""

	title: str
	html: str
	indicator_color: str | None = None


class PayPalSettings(Document):
	supported_currencies = (
		"AUD",
		"BRL",
		"CAD",
		"CNY",
		"CZK",
		"DKK",
		"EUR",
		"HKD",
		"HUF",
		"ILS",
		"JPY",
		"MYR",
		"MXN",
		"TWD",
		"NZD",
		"NOK",
		"PHP",
		"PLN",
		"GBP",
		"SGD",
		"SEK",
		"CHF",
		"THB",
		"USD",
	)

	classic_currencies = (
		"AUD",
		"BRL",
		"CAD",
		"CZK",
		"DKK",
		"EUR",
		"HKD",
		"HUF",
		"ILS",
		"JPY",
		"MYR",
		"MXN",
		"TWD",
		"NZD",
		"NOK",
		"PHP",
		"PLN",
		"GBP",
		"RUB",
		"SGD",
		"SEK",
		"CHF",
		"THB",
		"TRY",
		"USD",
	)

	def __setup__(self):
		self.use_sandbox = 0

	@property
	def uses_rest_api(self) -> bool:
		return bool(self.client_id and self.client_secret)

	@property
	def test_mode(self) -> bool:
		return bool(cint(self.paypal_sandbox))

	@property
	def api_host(self) -> str:
		return SANDBOX_HOST if self.test_mode else LIVE_HOST

	def setup_sandbox_env(self, token):
		data = json.loads(frappe.db.get_value("Integration Request", token, "data"))
		self.use_sandbox = cint(frappe._dict(data).use_sandbox) or 0

	def validate(self):
		create_payment_gateway("PayPal")
		call_hook_method("payment_gateway_enabled", gateway="PayPal")
		if self.flags.ignore_mandatory:
			return
		if not self.uses_rest_api and not self.api_username:
			frappe.throw(_("Enter the Client ID and Client Secret of your PayPal app"))
		if self.uses_rest_api:
			self.validate_rest_credentials()
		if self.api_username:
			self.validate_paypal_credentails()

	def on_update(self):
		pass

	def validate_transaction_currency(self, currency):
		currencies = self.supported_currencies if self.uses_rest_api else self.classic_currencies
		if currency not in currencies:
			frappe.throw(
				_(
					"Please select another payment method. PayPal does not support transactions in currency '{0}'"
				).format(currency)
			)

	def get_paypal_params_and_url(self):
		params = {
			"USER": self.api_username,
			"PWD": self.get_password(fieldname="api_password", raise_exception=False),
			"SIGNATURE": self.signature,
			"VERSION": "98",
			"METHOD": "GetPalDetails",
		}

		if hasattr(self, "use_sandbox") and self.use_sandbox:
			params.update(
				{
					"USER": frappe.conf.sandbox_api_username,
					"PWD": frappe.conf.sandbox_api_password,
					"SIGNATURE": frappe.conf.sandbox_signature,
				}
			)

		api_url = (
			"https://api-3t.sandbox.paypal.com/nvp"
			if (self.paypal_sandbox or self.use_sandbox)
			else "https://api-3t.paypal.com/nvp"
		)

		return params, api_url

	def validate_paypal_credentails(self):
		params, url = self.get_paypal_params_and_url()
		params = urlencode(params)

		try:
			res = make_post_request(url=url, data=params.encode("utf-8"))

			if res["ACK"][0] == "Failure":
				raise Exception

		except Exception:
			frappe.throw(_("Invalid payment gateway credentials"))

	def get_payment_url(self, **kwargs):
		if kwargs.get("subscription_details"):
			if not self.api_username:
				frappe.throw(
					_(
						"Recurring PayPal payments use PayPal's classic API: enter its API Username, API Password and Signature in PayPal Settings"
					)
				)
			return self.get_classic_payment_url(**kwargs)
		if not self.uses_rest_api:
			return self.get_classic_payment_url(**kwargs)
		return get_url(
			f"{api_path}.pay?" + urlencode({"integration_request": self.order_request_for(kwargs)})
		)

	def get_classic_payment_url(self, **kwargs):
		self.use_sandbox = cint(kwargs.get("use_sandbox", 0))

		response = self.execute_set_express_checkout(**kwargs)

		if self.paypal_sandbox or self.use_sandbox:
			return_url = "https://www.sandbox.paypal.com/cgi-bin/webscr?cmd=_express-checkout&token={0}"
		else:
			return_url = "https://www.paypal.com/cgi-bin/webscr?cmd=_express-checkout&token={0}"

		kwargs.update(
			{
				"token": response.get("TOKEN")[0],
				"correlation_id": response.get("CORRELATIONID")[0],
			}
		)

		create_request_log(kwargs, service_name="PayPal", name=kwargs["token"])

		return return_url.format(kwargs["token"])

	def execute_set_express_checkout(self, **kwargs):
		params, url = self.get_paypal_params_and_url()

		params.update(
			{
				"METHOD": "SetExpressCheckout",
				"returnUrl": get_url(f"{api_path}.get_express_checkout_details"),
				"cancelUrl": get_url("/payment-cancel"),
				"PAYMENTREQUEST_0_PAYMENTACTION": "SALE",
				"PAYMENTREQUEST_0_AMT": kwargs["amount"],
				"PAYMENTREQUEST_0_CURRENCYCODE": kwargs["currency"].upper(),
			}
		)

		if kwargs.get("subscription_details"):
			self.configure_recurring_payments(params, kwargs)

		params = urlencode(params)
		response = make_post_request(url, data=params.encode("utf-8"))

		if response.get("ACK")[0] != "Success":
			create_request_log(response, service_name="PayPal", status="Failed")
			frappe.throw(_("Looks like something is wrong with this site's Paypal configuration."))

		return response

	def configure_recurring_payments(self, params, kwargs):
		# removing the params as we have to setup rucurring payments
		for param in (
			"PAYMENTREQUEST_0_PAYMENTACTION",
			"PAYMENTREQUEST_0_AMT",
			"PAYMENTREQUEST_0_CURRENCYCODE",
		):
			del params[param]

		params.update(
			{
				"L_BILLINGTYPE0": "RecurringPayments",  # The type of billing agreement
				"L_BILLINGAGREEMENTDESCRIPTION0": kwargs["description"],
			}
		)

	# --- REST API -------------------------------------------------------------------------

	def access_token(self, fresh: bool = False) -> str:
		key = token_cache_key(self.api_host, self.client_id)
		token = None if fresh else frappe.cache.get_value(key, expires=True)
		if token:
			return token
		response = http_request(
			"POST",
			f"{self.api_host}/v1/oauth2/token",
			auth=(self.client_id, self.get_password(fieldname="client_secret", raise_exception=False)),
			data={"grant_type": "client_credentials"},
			headers={"Accept": "application/json"},
		)
		body = response_body(response)
		if response.status_code != 200 or not body.get("access_token"):
			raise PayPalError(response.status_code, body)
		# kept until a little before PayPal expires it
		frappe.cache.set_value(
			key, body["access_token"], expires_in_sec=max(cint(body.get("expires_in")) - 300, 60)
		)
		return body["access_token"]

	def api(self, method: str, path: str, payload=None, data=None, headers=None) -> dict:
		"""Call PayPal's REST API, with a new access token when PayPal no longer takes the cached one."""
		for fresh in (False, True):
			response = http_request(
				method,
				self.api_host + path,
				json=payload,
				data=data,
				headers={
					"Authorization": f"Bearer {self.access_token(fresh)}",
					"Content-Type": "application/json",
					"Accept": "application/json",
					**(headers or {}),
				},
			)
			if response.status_code != 401:
				break
		body = response_body(response)
		if response.status_code >= 400:
			raise PayPalError(response.status_code, body)
		return body

	def validate_rest_credentials(self):
		try:
			self.access_token(fresh=True)
		except Exception:
			frappe.throw(
				_(
					"PayPal didn't accept the Client ID and Client Secret. Check they are from a {0} app."
				).format(_("sandbox") if self.test_mode else _("live"))
			)

	def order_request_for(self, details: dict) -> str:
		"""The Integration Request a payer pays through. A document's is made once and reused until
		it's paid."""
		data = frappe._dict(details)
		if data.reference_doctype and data.reference_docname:
			reference = get_reference(data.reference_doctype, data.reference_docname)
			if reference and reference.doctype == "Payment Request":
				if reference.docstatus:
					data.amount, data.currency = amount_to_pay(reference)
				# else ERPNext asks for the link while submitting the request; what is owed is checked
				# when the link is opened
			existing = frappe.db.get_value(
				"Integration Request",
				{
					"integration_request_service": "PayPal",
					"request_description": ORDER,
					"reference_doctype": data.reference_doctype,
					"reference_docname": data.reference_docname,
					"status": "Queued",
				},
				"name",
				order_by="creation desc",
			)
			if existing:
				merged = json.loads(frappe.db.get_value("Integration Request", existing, "data") or "{}")
				merged.update(data)
				frappe.db.set_value(
					"Integration Request", existing, "data", frappe.as_json(merged), update_modified=False
				)
				return existing
		self.validate_transaction_currency((data.currency or "").upper())
		request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "PayPal",
				"request_description": ORDER,
				"status": "Queued",
				"reference_doctype": data.reference_doctype,
				"reference_docname": data.reference_docname,
				"data": frappe.as_json(data),
			}
		).insert(ignore_permissions=True)
		return request.name

	def amount_due(self, request) -> dict:
		"""What the payer is asked to pay through an Integration Request: for a Payment Request,
		what it still asks."""
		data = frappe._dict(json.loads(request.data or "{}"))
		reference = get_reference(request.reference_doctype, request.reference_docname)
		if reference and reference.doctype == "Payment Request":
			return paypal_amount(*amount_to_pay(reference))
		if reference and reference.docstatus == 2:
			frappe.throw(
				_("{0} {1} was cancelled").format(_(reference.doctype), reference.name), exc=NotPayable
			)
		if flt(data.amount) <= 0:
			frappe.throw(_("There is nothing to pay"), exc=NotPayable)
		return paypal_amount(data.amount, data.currency)

	def can_try_again(self, request) -> bool:
		"""Whether a payer can pay again after an attempt that took no money (declined, or not
		captured), while the document still asks to be paid."""
		if request.status not in ("Failed", "Cancelled"):
			return False
		taken = json.loads(frappe.db.get_value("Integration Request", request.name, "output") or "{}")
		if taken.get("status") == "COMPLETED":
			return False
		try:
			self.amount_due(request)
		except NotPayable:
			frappe.clear_last_message()
			return False
		return True

	def order(self, order_id: str | None) -> dict | None:
		if not order_id:
			return None
		try:
			return self.api("GET", f"/v2/checkout/orders/{quote(order_id, safe='')}")
		except PayPalError as e:
			if e.status_code == 404:
				return None
			raise

	def checkout(self, request) -> str | Message:
		"""Where a payer goes to pay: PayPal's page to approve an order for what is still asked."""
		if self.can_try_again(request):
			# nothing was taken and the document still asks to be paid: the link pays it anew
			details = json.loads(request.data or "{}")
			details.update(
				reference_doctype=request.reference_doctype, reference_docname=request.reference_docname
			)
			request = frappe.get_doc("Integration Request", self.order_request_for(details))
		if request.status != "Queued":
			return self.finished(request)
		order = self.order(request.request_id)
		if order and order.get("status") in ("APPROVED", "COMPLETED"):
			return self.complete(request)
		amount = self.amount_due(request)
		if not (
			order
			and order.get("status") in ("CREATED", "PAYER_ACTION_REQUIRED")
			and same_amount(order_amount(order), amount)
		):
			order = self.create_order(request, amount)
		return approve_link(order)

	def create_order(self, request, amount: dict) -> dict:
		data = frappe._dict(json.loads(request.data or "{}"))
		name = request.reference_docname or request.name
		query = urlencode({"integration_request": request.name})
		experience = {
			"return_url": get_url(f"{api_path}.complete_payment?{query}"),
			"cancel_url": get_url(f"{api_path}.cancel_payment?{query}"),
			"user_action": "PAY_NOW",
			"shipping_preference": "NO_SHIPPING",
		}
		if data.title:
			experience["brand_name"] = cstr(data.title)[:127]
		paypal = {"experience_context": experience}
		reference = get_reference(request.reference_doctype, request.reference_docname)
		# ERPNext names the user who made a Payment Request as its payer when it has no email
		email = (
			reference.get("email_to")
			if reference and reference.doctype == "Payment Request"
			else data.payer_email
		)
		if validate_email_address(cstr(email)):
			paypal["email_address"] = email
		order = self.api(
			"POST",
			"/v2/checkout/orders",
			payload={
				"intent": "CAPTURE",
				"purchase_units": [
					{
						"reference_id": name[:256],
						"custom_id": name[:127],
						"invoice_id": name[:127],
						"description": cstr(data.description or data.title or name)[:127],
						"amount": amount,
					}
				],
				"payment_source": {"paypal": paypal},
			},
			headers={
				"PayPal-Request-Id": request_key(
					"order", request.name, amount["currency_code"], amount["value"], request.request_id
				)
			},
		)
		data.update(
			{"order": order["id"], "amount": flt(amount["value"]), "currency": amount["currency_code"]}
		)
		request.db_set({"request_id": order["id"], "data": frappe.as_json(data)}, update_modified=False)
		return order

	def complete(self, request) -> str | Message:
		"""Capture the order a payer approved and record the payment. Called by the payer's return
		and PayPal's webhook alike, in any order."""
		if request.status != "Queued":
			return self.finished(request)
		order = self.order(request.request_id)
		if not order:
			return "/payment-failed"
		if order.get("status") in ("CREATED", "PAYER_ACTION_REQUIRED"):
			return approve_link(order)
		asked = order_amount(order)
		if order.get("status") == "APPROVED":
			refused = self.refuse_capture(request, order)
			if refused:
				return refused
			data = json.loads(request.data or "{}")
			try:
				order = self.api(
					"POST",
					f"/v2/checkout/orders/{quote(order['id'], safe='')}/capture",
					headers={
						"PayPal-Request-Id": request_key("capture", order["id"], data.get("declined")),
						"Prefer": "return=representation",
					},
				)
			except PayPalError as e:
				if e.issue == "ORDER_ALREADY_CAPTURED":
					order = self.order(order["id"]) or order
				elif e.issue in ("INSTRUMENT_DECLINED", "PAYER_ACTION_REQUIRED"):
					# PayPal asks the payer to choose another way to pay, and the capture is tried again
					data["declined"] = cint(data.get("declined")) + 1
					request.db_set("data", frappe.as_json(data), update_modified=False)
					return approve_link(order, cint(self.paypal_sandbox))
				else:
					raise
		return self.record_order(request, order, asked)

	def refuse_capture(self, request, order: dict) -> str | Message | None:
		"""Where to send a payer whose approved order isn't to be captured: its document can't be
		paid any more, or now asks for a different amount."""
		try:
			amount = self.amount_due(request)
		except NotPayable:
			frappe.clear_last_message()
			request.db_set("status", "Cancelled", update_modified=False)
			return Message(_("Payment not taken"), _("This can no longer be paid. You haven't been charged."))
		if not same_amount(order_amount(order), amount):
			# the payer approves a new order for what is asked now
			return approve_link(self.create_order(request, amount))
		return None

	def record_order(self, request, order: dict, asked: dict | None = None) -> str | Message:
		"""Record what PayPal captured for an order against the document it pays, once."""
		unit = (order.get("purchase_units") or [{}])[0]
		captures = (unit.get("payments") or {}).get("captures") or []
		if not captures:
			return "/payment-failed"
		capture = captures[-1]
		# the status is read with the lock: the payer's return and PayPal's webhook can arrive at
		# the same moment
		request.status = frappe.db.get_value("Integration Request", request.name, "status", for_update=True)
		if request.status != "Queued":
			return self.finished(request)
		paid = {
			"order": order.get("id"),
			"capture": capture.get("id"),
			"status": capture.get("status"),
			"amount": flt((capture.get("amount") or {}).get("value")),
			"currency": (capture.get("amount") or {}).get("currency_code"),
			"payer_email": (order.get("payer") or {}).get("email_address"),
		}
		if capture.get("status") == "PENDING":
			request.db_set("output", frappe.as_json(paid), update_modified=False)
			return Message(
				_("Payment pending"),
				_("PayPal is still processing your payment. It is recorded as soon as PayPal completes it."),
			)
		if capture.get("status") != "COMPLETED":
			request.db_set(
				{
					"status": "Failed",
					"output": frappe.as_json(paid),
					"error": _("PayPal didn't complete the payment ({0})").format(capture.get("status")),
				},
				update_modified=False,
			)
			return "/payment-failed"
		# two orders for one document can be paid at the same moment: the document is read with a
		# lock, so the second is checked against what the first left to pay
		reference = get_reference(request.reference_doctype, request.reference_docname, for_update=True)
		if reference and reference.docstatus == 2:
			# paid in the moment the document was cancelled: kept for staff to refund or record
			request.db_set(
				{
					"status": "Failed",
					"output": frappe.as_json(paid),
					"error": _("Paid after {0} {1} was cancelled. Refund it in PayPal, or record it.").format(
						_(reference.doctype), reference.name
					),
				},
				update_modified=False,
			)
			frappe.log_error(title="PayPal payment for a cancelled document", message=paid["capture"])
			tell_staff(
				reference,
				_(
					"A PayPal payment of {0} came in after {1} {2} was cancelled. Refund it in PayPal, or record it."
				).format(money(capture.get("amount")), _(reference.doctype), reference.name),
			)
			return Message(
				_("Payment received after cancellation"),
				_(
					"Your payment came in after this was cancelled. We've told our team, and they'll refund it."
				),
			)
		# orders are only made here, for what the document asked, so the payment must be the
		# order's whole amount, no more than the document still asks
		if not same_amount(capture.get("amount"), asked or unit.get("amount")) or more_than_asked(
			reference, capture.get("amount")
		):
			request.db_set(
				{
					"status": "Failed",
					"output": frappe.as_json(paid),
					"error": _("The amount paid doesn't match"),
				},
				update_modified=False,
			)
			frappe.log_error(title="PayPal payment doesn't match its document", message=paid["capture"])
			if reference:
				tell_staff(
					reference,
					_(
						"A PayPal payment of {0} doesn't match what {1} {2} asks, so it wasn't recorded. Refund it in PayPal, or record it."
					).format(money(capture.get("amount")), _(reference.doctype), reference.name),
				)
			return Message(
				_("Payment received"),
				_(
					"We received your payment, but it doesn't match what was asked. We've told our team, and they'll sort it out."
				),
			)
		request.db_set({"status": "Completed", "output": frappe.as_json(paid)}, update_modified=False)
		redirect_to = reference.run_method("on_payment_authorized", "Completed") if reference else None
		request.db_set("output", frappe.as_json({**paid, "redirect_to": redirect_to}), update_modified=False)
		return self.success_location(request, redirect_to)

	def finished(self, request) -> str | Message:
		"""Where to send a payer coming back to a payment that's already settled."""
		if request.status == "Completed":
			# recorded already: the payer is sent where they were sent then
			paid = json.loads(frappe.db.get_value("Integration Request", request.name, "output") or "{}")
			return self.success_location(request, paid.get("redirect_to"))
		if request.status == "Cancelled":
			return Message(_("Payment not taken"), _("This can no longer be paid. You haven't been charged."))
		return "/payment-failed"

	def success_location(self, request, redirect_to: str | None = None) -> str:
		data = frappe._dict(json.loads(request.data or "{}"))
		query = {
			"doctype": request.reference_doctype,
			"docname": request.reference_docname,
			"redirect_to": redirect_to or data.redirect_to or self.redirect_to,
			"redirect_message": data.redirect_message,
		}
		return "/payment-success?" + urlencode({key: value for key, value in query.items() if value})

	def refunds(self, paid) -> list[dict]:
		"""The refunds of a PayPal payment, made here or in PayPal, that went through or may yet."""
		order = self.api("GET", f"/v2/checkout/orders/{quote(paid.order, safe='')}")
		payments = (order.get("purchase_units") or [{}])[0].get("payments") or {}
		return [
			refund
			for refund in payments.get("refunds") or []
			if refund.get("status") in ("COMPLETED", "PENDING")
			and up_link_id(refund, "/captures/") in (None, paid.capture)
		]

	def refund(self, reference_doctype: str, reference_docname: str, amount: float, key: str) -> str:
		"""Refund part or all of what a document's PayPal payment took, once per key (e.g. the
		credit note it is for). For apps that refund on a staff action; returns PayPal's refund id."""
		if frappe.session.user == "Guest":
			raise frappe.PermissionError
		if flt(amount) <= 0:
			frappe.throw(_("The amount to refund must be more than zero"))
		request = frappe.db.get_value(
			"Integration Request",
			{
				"integration_request_service": "PayPal",
				"request_description": ORDER,
				"reference_doctype": reference_doctype,
				"reference_docname": reference_docname,
				"status": "Completed",
			},
			["name", "output"],
			as_dict=True,
			order_by="creation desc",
		)
		paid = frappe._dict(json.loads((request and request.output) or "{}"))
		if not paid.capture or not self.uses_rest_api:
			frappe.throw(
				_("{0} {1} has no PayPal payment to refund here. Refund it in PayPal.").format(
					_(reference_doctype), reference_docname
				)
			)
		for made in frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "PayPal",
				"request_description": REFUND,
				"reference_doctype": "Integration Request",
				"reference_docname": request.name,
			},
			fields=["request_id", "data"],
		):
			if json.loads(made.data or "{}").get("key") == key:
				return made.request_id
		value = paypal_amount(amount, paid.currency)
		refunds = self.refunds(paid)
		# the log of a refund made for this key is undone with the document refunding, and PayPal
		# forgets a PayPal-Request-Id: the refund PayPal holds for the key is the answer then
		refund = next((made for made in refunds if made.get("custom_id") == refund_mark(key)), None)
		if refund:
			value = {**value, **(refund.get("amount") or {})}
		else:
			left = flt(paid.amount) - sum(flt((made.get("amount") or {}).get("value")) for made in refunds)
			if flt(value["value"]) > left + 0.005:
				frappe.throw(
					_(
						"Only {0} of this PayPal payment can still be refunded: the rest was refunded in PayPal. Record that refund in ERPNext, then refund what is left."
					).format(money(paypal_amount(left, paid.currency)))
				)
			refund = self.api(
				"POST",
				f"/v2/payments/captures/{quote(paid.capture, safe='')}/refund",
				payload={"amount": value, "custom_id": refund_mark(key)},
				headers={
					"PayPal-Request-Id": request_key("refund", paid.capture, key, value["value"]),
					"Prefer": "return=representation",
				},
			)
			if refund.get("status") in ("CANCELLED", "FAILED"):
				frappe.throw(_("PayPal didn't make the refund ({0})").format(refund.get("status")))
		# logged against the payment it refunds, in the caller's transaction
		frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "PayPal",
				"request_description": REFUND,
				"request_id": refund["id"],
				"status": "Completed",
				"reference_doctype": "Integration Request",
				"reference_docname": request.name,
				"data": frappe.as_json(
					{
						"refund": refund["id"],
						"capture": paid.capture,
						"amount": flt(value["value"]),
						"currency": value["currency_code"],
						"key": key,
					}
				),
			}
		).insert(ignore_permissions=True)
		return refund["id"]

	def verify_webhook(self, body: str) -> bool:
		"""Whether PayPal says it sent a webhook event, for the webhook with this Webhook ID."""
		headers = {field: frappe.get_request_header(header) for field, header in SIGNATURE_HEADERS.items()}
		if not (self.uses_rest_api and self.webhook_id and all(headers.values())):
			return False
		# PayPal is asked about every event: one whose certificate isn't PayPal's is refused first
		cert = urlparse(headers["cert_url"])
		if cert.scheme != "https" or not (cert.hostname or "").endswith(".paypal.com"):
			return False
		try:
			event = json.loads(body)
		except ValueError:
			return False
		if not isinstance(event, dict) or not event.get("id"):
			return False
		# the event goes back as it came: encoding it again can change what PayPal signed
		payload = json.dumps({**headers, "webhook_id": self.webhook_id})[:-1] + f', "webhook_event": {body}}}'
		try:
			result = self.api("POST", "/v1/notifications/verify-webhook-signature", data=payload)
		except PayPalError as e:
			if e.status_code >= 500:
				raise
			return False
		return result.get("verification_status") == "SUCCESS"

	def handle_event(self, event: dict) -> str | None:
		"""Act on a webhook event PayPal sent. Returns the Integration Request of the payment it is
		about."""
		kind, resource = event.get("event_type"), event.get("resource") or {}
		if kind in ("CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.DENIED"):
			if kind == "CHECKOUT.ORDER.APPROVED":
				order_id = resource.get("id")
			else:
				order_id = ((resource.get("supplementary_data") or {}).get("related_ids") or {}).get(
					"order_id"
				)
			request = order_request(order_id=order_id)
			if not request:
				return None
			self.complete(request)
			return request.name
		if kind in ("PAYMENT.CAPTURE.REFUNDED", "PAYMENT.CAPTURE.REVERSED"):
			capture_id = up_link_id(resource, "/captures/")
			if not capture_id and event.get("resource_type") == "capture":
				capture_id = resource.get("id")
			return self.record_refund(kind, resource, self.request_for_capture(capture_id))
		if kind in ("CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.RESOLVED"):
			transactions = resource.get("disputed_transactions") or [{}]
			request = self.request_for_capture(transactions[0].get("seller_transaction_id"))
			return self.record_dispute(kind, resource, request)
		return None

	def request_for_capture(self, capture_id: str | None):
		"""The Integration Request of the order a PayPal capture paid."""
		if not capture_id:
			return None
		try:
			capture = self.api("GET", f"/v2/payments/captures/{quote(capture_id, safe='')}")
		except PayPalError as e:
			if e.status_code == 404:
				return None
			raise
		related = (capture.get("supplementary_data") or {}).get("related_ids") or {}
		return order_request(order_id=related.get("order_id"))

	def record_refund(self, kind: str, resource: dict, request) -> str | None:
		"""Tell staff about money PayPal gave back from a payment that wasn't refunded here."""
		if not request:
			return None
		# a refund made here is marked as such and logged; one made in PayPal carries the payment's
		# custom id (its document's name)
		custom_id = cstr(resource.get("custom_id"))
		if kind == "PAYMENT.CAPTURE.REFUNDED" and (
			custom_id.startswith(REFUND_MARK)
			or frappe.db.exists(
				"Integration Request",
				{"integration_request_service": "PayPal", "request_id": resource.get("id")},
			)
		):
			return request.name
		reference = get_reference(request.reference_doctype, request.reference_docname)
		if not reference:
			return request.name
		if kind == "PAYMENT.CAPTURE.REVERSED":
			note = _(
				"PayPal took back {0} of the PayPal payment for {1} {2}. Record the loss in ERPNext, and don't refund it again."
			)
		else:
			note = _(
				"{0} of the PayPal payment for {1} {2} was refunded in PayPal. Record the refund in ERPNext, and don't refund it again."
			)
		tell_staff(
			reference, note.format(money(resource.get("amount")), _(reference.doctype), reference.name)
		)
		return request.name

	def record_dispute(self, kind: str, dispute: dict, request) -> str | None:
		"""Tell staff about a payment the customer disputed with PayPal, on the documents it paid.
		Disputes are answered in PayPal; what was lost is recorded by staff."""
		if not request:
			return None
		reference = get_reference(request.reference_doctype, request.reference_docname)
		if not reference:
			return request.name
		amount = money(dispute.get("dispute_amount"))
		if kind == "CUSTOMER.DISPUTE.CREATED":
			due = dispute.get("seller_response_due_date")
			note = _(
				"The customer opened a PayPal dispute over {0} ({1}). Respond in PayPal's Resolution Center by {2}, or PayPal may decide it for them."
			).format(
				amount,
				cstr(dispute.get("reason") or "other").replace("_", " ").lower(),
				format_paypal_time(due) if due else _("the date PayPal gives"),
			)
		else:
			outcome = (dispute.get("dispute_outcome") or {}).get("outcome_code")
			if outcome in ("RESOLVED_SELLER_FAVOUR", "CANCELED_BY_BUYER", "DENIED"):
				note = _("The PayPal dispute over {0} was closed in your favour: the payment stands.").format(
					amount
				)
			elif outcome in ("RESOLVED_BUYER_FAVOUR", "ACCEPTED"):
				note = _(
					"The customer won their PayPal dispute over {0}: PayPal has given them the money back. Record the loss in ERPNext, and don't refund it again."
				).format(amount)
			else:
				note = _(
					"The PayPal dispute over {0} was closed ({1}). See what was paid back in PayPal's Resolution Center, and record it in ERPNext."
				).format(amount, cstr(outcome or "resolved").replace("_", " ").lower())
		tell_staff(reference, note)
		return request.name


def get_paypal_and_transaction_details(token):
	doc = frappe.get_doc("PayPal Settings")
	doc.setup_sandbox_env(token)
	params, url = doc.get_paypal_params_and_url()

	integration_request = frappe.get_doc("Integration Request", token)
	data = json.loads(integration_request.data)

	return data, params, url


def setup_redirect(data, redirect_url, custom_redirect_to=None, redirect=True):
	redirect_to = data.get("redirect_to") or None
	redirect_message = data.get("redirect_message") or None

	if custom_redirect_to:
		redirect_to = custom_redirect_to

	if redirect_to:
		redirect_url += "&" + urlencode({"redirect_to": redirect_to})
	if redirect_message:
		redirect_url += "&" + urlencode({"redirect_message": redirect_message})

	# this is done so that functions called via hooks can update flags.redirect_to
	if redirect:
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_url(redirect_url)


@frappe.whitelist(allow_guest=True, xss_safe=True)
def get_express_checkout_details(token):
	try:
		doc = frappe.get_doc("PayPal Settings")
		doc.setup_sandbox_env(token)

		params, url = doc.get_paypal_params_and_url()
		params.update({"METHOD": "GetExpressCheckoutDetails", "TOKEN": token})

		response = make_post_request(url, data=params)

		if response.get("ACK")[0] != "Success":
			frappe.respond_as_web_page(
				_("Something went wrong"),
				_(
					"Looks like something went wrong during the transaction. Since we haven't confirmed the payment, Paypal will automatically refund you this amount. If it doesn't, please send us an email and mention the Correlation ID: {0}."
				).format(response.get("CORRELATIONID", [None])[0]),
				indicator_color="red",
				http_status_code=frappe.ValidationError.http_status_code,
			)

			return

		doc = frappe.get_doc("Integration Request", token)
		update_integration_request_status(
			token,
			{"payerid": response.get("PAYERID")[0], "payer_email": response.get("EMAIL")[0]},
			"Authorized",
			doc=doc,
		)

		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = get_redirect_uri(doc, token, response.get("PAYERID")[0])

	except Exception:
		frappe.log_error(frappe.get_traceback())


@frappe.whitelist(allow_guest=True, xss_safe=True)
def confirm_payment(token):
	try:
		custom_redirect_to = None
		data, params, url = get_paypal_and_transaction_details(token)

		params.update(
			{
				"METHOD": "DoExpressCheckoutPayment",
				"PAYERID": data.get("payerid"),
				"TOKEN": token,
				"PAYMENTREQUEST_0_PAYMENTACTION": "SALE",
				"PAYMENTREQUEST_0_AMT": data.get("amount"),
				"PAYMENTREQUEST_0_CURRENCYCODE": data.get("currency").upper(),
			}
		)

		response = make_post_request(url, data=params)

		if response.get("ACK")[0] == "Success":
			update_integration_request_status(
				token,
				{
					"transaction_id": response.get("PAYMENTINFO_0_TRANSACTIONID")[0],
					"correlation_id": response.get("CORRELATIONID")[0],
				},
				"Completed",
			)

			if data.get("reference_doctype") and data.get("reference_docname"):
				custom_redirect_to = frappe.get_doc(
					data.get("reference_doctype"), data.get("reference_docname")
				).run_method("on_payment_authorized", "Completed")
				frappe.db.commit()

			redirect_url = "payment-success?doctype={}&docname={}".format(
				data.get("reference_doctype"), data.get("reference_docname")
			)
		else:
			redirect_url = "payment-failed"

		setup_redirect(data, redirect_url, custom_redirect_to)

	except Exception:
		frappe.log_error(frappe.get_traceback())


@frappe.whitelist(allow_guest=True, xss_safe=True)
def create_recurring_profile(token, payerid):
	try:
		custom_redirect_to = None
		updating = False
		data, params, url = get_paypal_and_transaction_details(token)

		addons = data.get("addons")
		subscription_details = data.get("subscription_details")

		if data.get("subscription_id"):
			if addons:
				updating = True
			manage_recurring_payment_profile_status(data["subscription_id"], "Cancel", params, url)

		params.update(
			{
				"METHOD": "CreateRecurringPaymentsProfile",
				"PAYERID": payerid,
				"TOKEN": token,
				"DESC": data.get("description"),
				"BILLINGPERIOD": subscription_details.get("billing_period"),
				"BILLINGFREQUENCY": subscription_details.get("billing_frequency"),
				"AMT": data.get("subscription_amount"),
				"CURRENCYCODE": data.get("currency").upper(),
				"INITAMT": data.get("upfront_amount"),
			}
		)

		status_changed_to = "Completed" if data.get("starting_immediately") or updating else "Verified"

		starts_at = get_datetime(subscription_details.get("start_date")) or frappe.utils.now_datetime()
		starts_at = starts_at.replace(tzinfo=ZoneInfo(get_system_timezone())).astimezone(ZoneInfo("UTC"))

		# "PROFILESTARTDATE": datetime.utcfromtimestamp(get_timestamp(starts_at)).isoformat()
		params.update({"PROFILESTARTDATE": starts_at.isoformat()})

		response = make_post_request(url, data=params)

		if response.get("ACK")[0] == "Success":
			update_integration_request_status(
				token,
				{
					"profile_id": response.get("PROFILEID")[0],
				},
				"Completed",
			)

			if data.get("reference_doctype") and data.get("reference_docname"):
				data["subscription_id"] = response.get("PROFILEID")[0]

				frappe.flags.data = data
				custom_redirect_to = frappe.get_doc(
					data.get("reference_doctype"), data.get("reference_docname")
				).run_method("on_payment_authorized", status_changed_to)
				frappe.db.commit()

			redirect_url = "payment-success?doctype={}&docname={}".format(
				data.get("reference_doctype"), data.get("reference_docname")
			)
		else:
			redirect_url = "payment-failed"

		setup_redirect(data, redirect_url, custom_redirect_to)

	except Exception:
		frappe.log_error(frappe.get_traceback())


def update_integration_request_status(token, data, status, error=False, doc=None):
	if not doc:
		doc = frappe.get_doc("Integration Request", token)

	doc.update_status(data, status)


def get_redirect_uri(doc, token, payerid):
	data = json.loads(doc.data)

	if data.get("subscription_details"):
		return get_url(f"{api_path}.create_recurring_profile?token={token}&payerid={payerid}")
	else:
		return get_url(f"{api_path}.confirm_payment?token={token}")


def manage_recurring_payment_profile_status(profile_id, action, args, url):
	args.update(
		{
			"METHOD": "ManageRecurringPaymentsProfileStatus",
			"PROFILEID": profile_id,
			"ACTION": action,
		}
	)

	response = make_post_request(url, data=args)

	# error code 11556 indicates profile is not in active state(or already cancelled)
	# thus could not cancel the subscription.
	# thus raise an exception only if the error code is not equal to 11556

	if response.get("ACK")[0] != "Success" and response.get("L_ERRORCODE0", [])[0] != "11556":
		frappe.throw(_("Failed while amending subscription"))


@frappe.whitelist(allow_guest=True)
def ipn_handler():
	try:
		data = frappe.local.form_dict

		validate_ipn_request(data)

		data.update({"payment_gateway": "PayPal"})

		doc = frappe.get_doc(
			{
				"data": json.dumps(frappe.local.form_dict),
				"doctype": "Integration Request",
				"request_description": "Subscription Notification",
				"is_remote_request": 1,
				"status": "Queued",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()

		frappe.enqueue(
			method="payments.payment_gateways.doctype.paypal_settings.paypal_settings.handle_subscription_notification",
			queue="long",
			timeout=600,
			is_async=True,
			**{"doctype": "Integration Request", "docname": doc.name},
		)

	except frappe.InvalidStatusError:
		pass
	except Exception as e:
		frappe.log(frappe.log_error(title=e))


def validate_ipn_request(data):
	def _throw():
		frappe.throw(_("In Valid Request"), exc=frappe.InvalidStatusError)

	if not data.get("recurring_payment_id"):
		_throw()

	doc = frappe.get_doc("PayPal Settings")
	params, url = doc.get_paypal_params_and_url()

	params.update(
		{
			"METHOD": "GetRecurringPaymentsProfileDetails",
			"PROFILEID": data.get("recurring_payment_id"),
		}
	)

	params = urlencode(params)
	res = make_post_request(url=url, data=params.encode("utf-8"))

	if res["ACK"][0] != "Success":
		_throw()


def handle_subscription_notification(doctype, docname):
	call_hook_method("handle_subscription_notification", doctype=doctype, docname=docname)


# --- REST API -----------------------------------------------------------------------------


def http_request(method: str, url: str, **kwargs):
	return requests.request(method, url, timeout=30, **kwargs)


def response_body(response) -> dict:
	try:
		return response.json() if response.content else {}
	except ValueError:
		return {}


def token_cache_key(host: str, client_id: str | None) -> str:
	return f"paypal_access_token:{host}:{client_id}"


REFUND_MARK = "refund:"


def refund_mark(key: str) -> str:
	"""The custom id of a refund made here: no document's name starts with it."""
	return f"{REFUND_MARK}{cstr(key)}"[:127]


def request_key(*parts) -> str:
	"""A PayPal-Request-Id: PayPal answers a request made again with the same id as it did the first time."""
	key = ":".join([frappe.local.site, *(cstr(part) for part in parts)])
	return hashlib.sha256(key.encode()).hexdigest()


def paypal_amount(amount, currency: str) -> dict:
	"""An amount as PayPal takes it: a string with as many decimals as the currency has."""
	currency = (currency or "").upper()
	exponent = Decimal(1) if currency in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
	value = Decimal(str(flt(amount))).quantize(exponent, rounding=ROUND_HALF_UP)
	return {"currency_code": currency, "value": str(value)}


def same_amount(a: dict | None, b: dict | None) -> bool:
	a, b = a or {}, b or {}
	return paypal_amount(a.get("value"), a.get("currency_code")) == paypal_amount(
		b.get("value"), b.get("currency_code")
	)


def more_than_asked(reference, amount: dict | None) -> bool:
	"""Whether a payment is more than its document asks: for a Payment Request, more than is left
	of it to pay."""
	if not reference or not reference.meta.has_field("grand_total"):
		return False
	if reference.doctype == "Payment Request":
		try:
			total = paypal_amount(*amount_to_pay(reference))
		except NotPayable:
			frappe.clear_last_message()
			return True
	else:
		total = paypal_amount(reference.get("grand_total"), reference.get("currency"))
	amount = paypal_amount((amount or {}).get("value"), (amount or {}).get("currency_code"))
	return amount["currency_code"] != total["currency_code"] or Decimal(amount["value"]) > Decimal(
		total["value"]
	)


def money(amount: dict | None) -> str:
	amount = amount or {}
	return fmt_money(flt(amount.get("value")), currency=amount.get("currency_code"))


def format_paypal_time(value: str) -> str:
	when = datetime.fromisoformat(value.replace("Z", "+00:00"))
	if when.tzinfo:
		when = when.astimezone(UTC).replace(tzinfo=None)
	return format_datetime(convert_utc_to_system_timezone(when))


def get_reference(doctype: str | None, name: str | None, for_update: bool = False):
	if doctype and name and frappe.db.exists(doctype, name):
		return frappe.get_doc(doctype, name, for_update=for_update)
	return None


def order_request(name: str | None = None, order_id: str | None = None):
	"""The Integration Request a payer pays a PayPal order through, by its name or the order's id."""
	if not (name or order_id):
		return None
	filters = {"integration_request_service": "PayPal", "request_description": ORDER}
	filters.update({"name": name} if name else {"request_id": order_id})
	found = frappe.db.get_value("Integration Request", filters, "name")
	return frappe.get_doc("Integration Request", found) if found else None


def order_amount(order: dict) -> dict | None:
	return ((order.get("purchase_units") or [{}])[0]).get("amount")


def approve_link(order: dict, sandbox: bool | None = None) -> str:
	for link in order.get("links") or []:
		if link.get("rel") in ("payer-action", "approve"):
			return link["href"]
	if order.get("id") and sandbox is not None:
		# an approved order whose payment was declined has no link: the payer approves it again
		host = "https://www.sandbox.paypal.com" if sandbox else "https://www.paypal.com"
		return f"{host}/checkoutnow?" + urlencode({"token": order["id"]})
	frappe.throw(_("PayPal didn't give a page to approve the payment on"))


def up_link_id(resource: dict, kind: str) -> str | None:
	"""The id of the resource a PayPal resource belongs to, e.g. the capture of a refund."""
	for link in resource.get("links") or []:
		href = link.get("href") or ""
		if link.get("rel") == "up" and kind in href:
			return href.rstrip("/").rsplit("/", 1)[-1]
	return None


def send_payer_to(outcome: str | Message) -> None:
	if isinstance(outcome, Message):
		# shown on the site's message page, which sets the redirect itself outside of Ajax
		outcome = frappe.redirect_to_message(
			outcome.title, outcome.html, indicator_color=outcome.indicator_color
		)
		if not outcome:
			return
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = get_url(outcome)


def respond(action, integration_request: str | None) -> None:
	"""Send the payer on to where an action on their payment leads."""
	request = order_request(name=integration_request)
	if not request:
		send_payer_to(Message(_("Payment not found"), _("This payment link isn't valid."), "red"))
		return
	try:
		outcome = action(frappe.get_doc("PayPal Settings"), request)
		# the payer arrives with a GET, which Frappe doesn't commit
		frappe.db.commit()  # nosemgrep
	except NotPayable as e:
		frappe.db.rollback()
		frappe.clear_last_message()
		outcome = Message(_("Payment not taken"), str(e))
	except Exception:
		frappe.db.rollback()
		frappe.log_error(title="PayPal payment")
		outcome = Message(
			_("Something went wrong"),
			_(
				"We couldn't complete your payment with PayPal. Please try again, or contact us if you were charged."
			),
			"red",
		)
	send_payer_to(outcome)


# Guest checkout: payers pay without a site account.
@frappe.whitelist(allow_guest=True, xss_safe=True)  # nosemgrep
def pay(integration_request: str):
	"""Send the payer to PayPal, to pay what is still asked."""
	respond(lambda settings, request: settings.checkout(request), integration_request)


# Guest checkout: PayPal sends payers back here, signed in or not.
@frappe.whitelist(allow_guest=True, xss_safe=True)  # nosemgrep
def complete_payment(integration_request: str):
	"""PayPal sends the payer here once they approve the payment, which is then taken."""
	respond(lambda settings, request: settings.complete(request), integration_request)


# Guest checkout: PayPal sends payers back here, signed in or not.
@frappe.whitelist(allow_guest=True, xss_safe=True)  # nosemgrep
def cancel_payment(integration_request: str):
	"""PayPal sends the payer here when they cancel."""
	message = _("You cancelled the payment at PayPal, so you haven't been charged.")
	request = order_request(name=integration_request)
	if request and request.status == "Queued":
		again = get_url(f"{api_path}.pay?" + urlencode({"integration_request": request.name}))
		message += '<p><a class="btn btn-primary btn-sm" href="{}">{}</a></p>'.format(
			escape_html(again), _("Pay with PayPal")
		)
	send_payer_to(Message(_("Payment cancelled"), message))


# PayPal calls this; the event is verified with PayPal for the webhook with the Webhook ID.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
@rate_limit(limit=100, seconds=60)
def webhook():
	"""Record payments whose payer didn't come back to the site, and tell staff about refunds made
	in PayPal, reversals and disputes."""
	settings = frappe.get_doc("PayPal Settings")
	body = frappe.request.get_data(as_text=True)
	if not settings.verify_webhook(body):
		raise frappe.PermissionError
	event = json.loads(body)
	# PayPal sends an event again until it's answered, at times twice at once: each is acted on once
	if not first_time("PayPal", event["id"], event):
		return {"received": True}
	about = settings.handle_event(event)
	frappe.db.set_value(
		"Integration Request",
		{"integration_request_service": "PayPal", "request_id": event["id"]},
		{
			"reference_doctype": "Integration Request" if about else None,
			"reference_docname": about,
			"request_description": event.get("event_type"),
			"is_remote_request": 1,
		},
		update_modified=False,
	)
	# webhooks are not committed otherwise
	frappe.db.commit()  # nosemgrep
	return {"received": True}
