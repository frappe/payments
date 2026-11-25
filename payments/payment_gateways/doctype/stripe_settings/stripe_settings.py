# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

import json
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.rate_limiter import rate_limit
from frappe.utils import (
	call_hook_method,
	cint,
	convert_utc_to_system_timezone,
	flt,
	fmt_money,
	format_datetime,
	get_url,
	validate_email_address,
)

from payments.utils import create_payment_gateway
from payments.utils.payment_request import amount_to_pay, first_time, tell_staff

currency_wise_minimum_charge_amount = {
	"JPY": 50,
	"MXN": 10,
	"DKK": 2.50,
	"HKD": 4.00,
	"NOK": 3.00,
	"SEK": 3.00,
	"USD": 0.50,
	"AUD": 0.50,
	"BRL": 0.50,
	"CAD": 0.50,
	"CHF": 0.50,
	"EUR": 0.50,
	"GBP": 0.30,
	"NZD": 0.50,
	"SGD": 0.50,
}


# currencies Stripe takes in whole units, and those it takes in thousandths
ZERO_DECIMAL_CURRENCIES = frozenset(
	(
		"BIF",
		"CLP",
		"DJF",
		"GNF",
		"JPY",
		"KMF",
		"KRW",
		"MGA",
		"PYG",
		"RWF",
		"UGX",
		"VND",
		"VUV",
		"XAF",
		"XOF",
		"XPF",
	)
)
THREE_DECIMAL_CURRENCIES = frozenset(("BHD", "JOD", "KWD", "OMR", "TND"))


def minor_units(amount: float, currency: str) -> int:
	"""An amount in the smallest unit Stripe charges in for the currency."""
	currency = currency.upper()
	if currency in ZERO_DECIMAL_CURRENCIES:
		return cint(round(flt(amount)))
	if currency in THREE_DECIMAL_CURRENCIES:
		# Stripe takes these to the nearest ten thousandths
		return cint(round(flt(amount) * 100)) * 10
	return cint(round(flt(amount) * 100))


def major_units(units: int, currency: str) -> float:
	"""An amount Stripe gives in the currency's smallest unit, in the currency."""
	currency = currency.upper()
	if currency in ZERO_DECIMAL_CURRENCIES:
		return flt(units)
	return flt(units) / (1000 if currency in THREE_DECIMAL_CURRENCIES else 100)


def key_mode(key: str | None) -> str | None:
	"""Whether a Stripe API key is a test or a live key: Stripe's test mode and sandboxes use the same
	API with their own keys."""
	for mode in ("test", "live"):
		if (key or "").startswith((f"pk_{mode}_", f"sk_{mode}_", f"rk_{mode}_")):
			return mode
	return None


class StripeSettings(Document):
	supported_currencies = (
		"AED",
		"ALL",
		"ANG",
		"ARS",
		"AUD",
		"AWG",
		"BBD",
		"BDT",
		"BIF",
		"BMD",
		"BND",
		"BOB",
		"BRL",
		"BSD",
		"BWP",
		"BZD",
		"CAD",
		"CHF",
		"CLP",
		"CNY",
		"COP",
		"CRC",
		"CVE",
		"CZK",
		"DJF",
		"DKK",
		"DOP",
		"DZD",
		"EGP",
		"ETB",
		"EUR",
		"FJD",
		"FKP",
		"GBP",
		"GIP",
		"GMD",
		"GNF",
		"GTQ",
		"GYD",
		"HKD",
		"HNL",
		"HRK",
		"HTG",
		"HUF",
		"IDR",
		"ILS",
		"INR",
		"ISK",
		"JMD",
		"JPY",
		"KES",
		"KHR",
		"KMF",
		"KRW",
		"KYD",
		"KZT",
		"LAK",
		"LBP",
		"LKR",
		"LRD",
		"MAD",
		"MDL",
		"MNT",
		"MOP",
		"MRO",
		"MUR",
		"MVR",
		"MWK",
		"MXN",
		"MYR",
		"NAD",
		"NGN",
		"NIO",
		"NOK",
		"NPR",
		"NZD",
		"PAB",
		"PEN",
		"PGK",
		"PHP",
		"PKR",
		"PLN",
		"PYG",
		"QAR",
		"RUB",
		"SAR",
		"SBD",
		"SCR",
		"SEK",
		"SGD",
		"SHP",
		"SLL",
		"SOS",
		"STD",
		"SVC",
		"SZL",
		"THB",
		"TOP",
		"TTD",
		"TWD",
		"TZS",
		"UAH",
		"UGX",
		"USD",
		"UYU",
		"UZS",
		"VND",
		"VUV",
		"WST",
		"XAF",
		"XOF",
		"XPF",
		"YER",
		"ZAR",
	)

	currency_wise_minimum_charge_amount = MappingProxyType(currency_wise_minimum_charge_amount)

	def validate(self):
		secret = self.get_password(fieldname="secret_key", raise_exception=False)
		modes = key_mode(self.publishable_key), key_mode(secret)
		if all(modes) and modes[0] != modes[1]:
			frappe.throw(
				_(
					"The Publishable Key is a {0} key and the Secret Key a {1} key. Use both from the same mode."
				).format(_(modes[0]), _(modes[1]))
			)

	@property
	def test_mode(self) -> bool:
		return key_mode(self.publishable_key) == "test"

	def on_update(self):
		create_payment_gateway(
			"Stripe-" + self.gateway_name,
			settings="Stripe Settings",
			controller=self.gateway_name,
		)
		call_hook_method("payment_gateway_enabled", gateway="Stripe-" + self.gateway_name)
		if not self.flags.ignore_mandatory:
			self.validate_stripe_credentails()

	def validate_stripe_credentails(self):
		if self.publishable_key and self.secret_key:
			header = {
				"Authorization": "Bearer {}".format(
					self.get_password(fieldname="secret_key", raise_exception=False)
				)
			}
			try:
				make_get_request(url="https://api.stripe.com/v1/charges", headers=header)
			except Exception:
				frappe.throw(_("Seems Publishable Key or Secret Key is wrong !!!"))

	def validate_transaction_currency(self, currency):
		if currency not in self.supported_currencies:
			frappe.throw(
				_(
					"Please select another payment method. Stripe does not support transactions in currency '{0}'"
				).format(currency)
			)

	def validate_minimum_transaction_amount(self, currency, amount):
		if currency in self.currency_wise_minimum_charge_amount:
			if flt(amount) < self.currency_wise_minimum_charge_amount.get(currency, 0.0):
				frappe.throw(
					_("For currency {0}, the minimum transaction amount should be {1}").format(
						currency, self.currency_wise_minimum_charge_amount.get(currency, 0.0)
					)
				)

	def get_payment_url(self, **kwargs):
		return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

	def create_request(self, data):
		import stripe

		self.data = frappe._dict(data)
		if self.data.reference_doctype == "Payment Request" and self.data.reference_docname:
			# what the request asks, never what the browser sent
			reference = frappe.get_doc(self.data.reference_doctype, self.data.reference_docname)
			self.data.amount, self.data.currency = amount_to_pay(reference)
		stripe.api_key = self.get_password(fieldname="secret_key", raise_exception=False)
		stripe.default_http_client = stripe.http_client.RequestsClient()

		try:
			self.integration_request = create_request_log(self.data, service_name="Stripe")
			return self.create_charge_on_stripe()

		except Exception:
			frappe.log_error(frappe.get_traceback())
			return {
				"redirect_to": frappe.redirect_to_message(
					_("Server Error"),
					_(
						"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
					),
				),
				"status": 401,
			}

	def create_charge_on_stripe(self):
		import stripe

		try:
			charge = stripe.Charge.create(
				amount=minor_units(self.data.amount, self.data.currency),
				currency=self.data.currency,
				source=self.data.stripe_token_id,
				description=self.data.description,
				receipt_email=self.data.payer_email,
			)

			if charge.captured is True:
				self.integration_request.db_set("status", "Completed", update_modified=False)
				self.flags.status_changed_to = "Completed"

			else:
				frappe.log_error(charge.failure_message, "Stripe Payment not completed")

		except Exception:
			frappe.log_error(frappe.get_traceback())

		return self.finalize_request()

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

				redirect_url = f"payment-success?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"

			if self.redirect_url:
				redirect_url = self.redirect_url
				redirect_to = None
		else:
			redirect_url = "payment-failed"

		if redirect_to:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode({"redirect_to": redirect_to})

		if redirect_message:
			redirect_url += ("&" if "?" in redirect_url else "?") + urlencode(
				{"redirect_message": redirect_message}
			)

		return {"redirect_to": redirect_url, "status": status}

	# --- PaymentIntents ---------------------------------------------------------------

	def client(self):
		import stripe

		return stripe.StripeClient(self.get_password(fieldname="secret_key", raise_exception=False))

	def payment_intent(self, reference_doctype: str, reference_docname: str):
		"""The PaymentIntent paying a document (usually a Payment Request), for what it still asks in
		its currency. It is made once and reused while the document is unpaid."""
		reference = frappe.get_doc(reference_doctype, reference_docname)
		client = self.client()
		existing = intent_request(reference_doctype, reference_docname)
		intent = client.payment_intents.retrieve(existing.request_id) if existing else None
		if intent and intent.status == "succeeded":
			if existing.status != "Failed":
				# paid: the caller records it, or sends the payer where it sent them before
				return intent
			# refused (and refunded by staff): what is still owed is paid with a new intent
			intent = None
		amount, currency = amount_to_pay(reference)
		self.validate_transaction_currency(currency)
		self.validate_minimum_transaction_amount(currency, amount)
		units = minor_units(amount, currency)
		if intent and intent.status != "canceled" and intent.currency == currency.lower():
			if intent.amount != units and intent.status != "processing":
				intent = client.payment_intents.update(intent.id, params={"amount": units})
			return intent
		attempt = frappe.db.count(
			"Integration Request",
			{
				"integration_request_service": "Stripe",
				"reference_doctype": reference_doctype,
				"reference_docname": reference_docname,
				"request_id": ["like", "pi_%"],
			},
		)
		if self.get_password(fieldname="webhook_secret", raise_exception=False):
			methods = {"automatic_payment_methods": {"enabled": True}}
		else:
			# without the webhook a payment is recorded only when the payer comes back, so only
			# cards (and the wallets that pay by card) are offered: bank debits settle days later
			methods = {"payment_method_types": ["card"]}
		intent = client.payment_intents.create(
			params={
				"amount": units,
				"currency": currency.lower(),
				**methods,
				# the pay page shows the intent to whoever has the link: the description names only
				# the document, never the customer
				"description": f"{_(reference_doctype)} {reference_docname}",
				# a request made without the customer's email holds the user who made it
				"receipt_email": validate_email_address(reference.get("email_to") or "") or None,
				"metadata": {
					"reference_doctype": reference_doctype,
					"reference_docname": reference_docname,
					"payment_gateway": reference.get("payment_gateway") or "",
					"site": frappe.local.site,
				},
			},
			options={
				"idempotency_key": f"{frappe.local.site}:{reference_doctype}:{reference_docname}:{units}:{attempt}"
			},
		)
		create_request_log(
			{"payment_intent": intent.id, "amount": amount, "currency": currency},
			service_name="Stripe",
			reference_doctype=reference_doctype,
			reference_docname=reference_docname,
			request_id=intent.id,
		)
		return intent

	def record_payment_intent(self, intent) -> frappe._dict | None:
		"""Record a PaymentIntent Stripe reports as succeeded against the document it pays, once.

		Returns whether it was recorded and where to send the payer next; None for an intent this
		site didn't make."""
		metadata = intent.metadata or {}
		reference_doctype, reference_docname = (
			metadata.get("reference_doctype"),
			metadata.get("reference_docname"),
		)
		if intent.status != "succeeded" or metadata.get("site") != frappe.local.site:
			return None
		# the status is read with the lock: a plain read can see the moment before the webhook or the
		# payer's return recorded the same payment
		locked = frappe.db.get_value(
			"Integration Request",
			{"integration_request_service": "Stripe", "request_id": intent.id},
			["name", "status", "output"],
			as_dict=True,
			for_update=True,
		)
		if not locked:
			return frappe._dict(recorded=False, location=not_recorded_location())
		request = frappe.get_doc("Integration Request", locked.name)
		request.status = locked.status
		if request.status == "Completed":
			# recorded already: the payer is sent where they were sent then
			stored = json.loads(locked.output or "{}").get("redirect_to")
			return frappe._dict(
				recorded=True, location=stored or success_location(reference_doctype, reference_docname)
			)
		if request.status == "Failed":
			return frappe._dict(recorded=False, location=not_recorded_location())
		reference = frappe.get_doc(reference_doctype, reference_docname)
		problem = self.payment_problem(intent, reference)
		if problem:
			request.db_set({"status": "Failed", "error": problem}, update_modified=False)
			frappe.log_error(title="Stripe payment not recorded", message=f"{intent.id}: {problem}")
			paid = fmt_money(
				major_units(intent.amount_received, intent.currency), currency=intent.currency.upper()
			)
			tell_staff(
				reference,
				_(
					"A card payment of {0} for {1} {2} wasn't recorded. {3} Refund it in Stripe, or record it."
				).format(paid, _(reference_doctype), reference_docname, problem),
			)
			return frappe._dict(recorded=False, location=not_recorded_location(reference.docstatus == 2))
		request.db_set("status", "Completed", update_modified=False)
		redirect_to = reference.run_method("on_payment_authorized", "Completed")
		request.db_set("output", json.dumps({"redirect_to": redirect_to}), update_modified=False)
		return frappe._dict(
			recorded=True, location=redirect_to or success_location(reference_doctype, reference_docname)
		)

	def payment_problem(self, intent, reference) -> str | None:
		"""Why a succeeded PaymentIntent can't be recorded against its document, if it can't."""
		if reference.doctype == "Payment Request":
			gateway = (intent.metadata or {}).get("payment_gateway")
			if get_gateway_controller(reference.doctype, reference.name) != self.name or (
				gateway and gateway != reference.get("payment_gateway")
			):
				return _("It was taken through another payment gateway.")
		if bool(intent.livemode) == self.test_mode:
			return _("It was taken in Stripe's {0} mode, and the gateway is in {1} mode.").format(
				_("live") if intent.livemode else _("test"), _("test") if self.test_mode else _("live")
			)
		if reference.docstatus == 2:
			return _("{0} {1} was cancelled before it was paid.").format(_(reference.doctype), reference.name)
		try:
			owed, currency = amount_to_pay(reference)
		except frappe.ValidationError:
			frappe.clear_messages()
			return _("{0} {1} had nothing left to pay.").format(_(reference.doctype), reference.name)
		if intent.currency != (currency or "").lower():
			return _("It was paid in {0}, and {1} {2} asks for {3}.").format(
				intent.currency.upper(), _(reference.doctype), reference.name, currency
			)
		if intent.amount_received < intent.amount or intent.amount_received > minor_units(owed, currency):
			return _("{0} {1} still asked for {2}.").format(
				_(reference.doctype), reference.name, fmt_money(owed, currency=currency)
			)
		return None

	def refund(self, reference_doctype: str, reference_docname: str, amount: float, key: str) -> str:
		"""Refund part or all of what a document's PaymentIntent took, once per key (e.g. the credit
		note it is for). For apps that refund on a staff action; returns the Stripe Refund id."""
		if frappe.session.user == "Guest":
			raise frappe.PermissionError
		if flt(amount) <= 0:
			frappe.throw(_("The amount to refund must be more than zero"))
		request = intent_request(reference_doctype, reference_docname)
		if not request or request.status != "Completed":
			frappe.throw(
				_("{0} {1} has no card payment to refund").format(_(reference_doctype), reference_docname)
			)
		currency = frappe.db.get_value(reference_doctype, reference_docname, "currency")
		units = minor_units(amount, currency)
		client = self.client()
		# Stripe forgets idempotency keys after a day: a refund made for this key before is reused
		refunds = client.refunds.list(params={"payment_intent": request.request_id, "limit": 100}).data
		for made in refunds:
			if (made.metadata or {}).get("key") == key and made.status != "failed":
				return made.id
		# refunds made here or in Stripe's dashboard
		left = client.payment_intents.retrieve(request.request_id).amount_received - sum(
			made.amount for made in refunds if made.status in ("succeeded", "pending")
		)
		if units > left:
			frappe.throw(
				_(
					"Only {0} of this card payment can still be refunded: the rest was refunded in Stripe. Record that refund in ERPNext, then refund what is left."
				).format(fmt_money(major_units(left, currency), currency=currency))
			)
		refund = client.refunds.create(
			params={
				"payment_intent": request.request_id,
				"amount": units,
				"metadata": {
					"reference_doctype": reference_doctype,
					"reference_docname": reference_docname,
					"key": key,
				},
			},
			options={"idempotency_key": f"{frappe.local.site}:refund:{request.request_id}:{key}:{units}"},
		)
		# logged against the payment it refunds, in the caller's transaction
		frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Stripe",
				"request_id": refund.id,
				"status": "Completed",
				"reference_doctype": "Integration Request",
				"reference_docname": request.name,
				"data": json.dumps(
					{
						"refund": refund.id,
						"payment_intent": request.request_id,
						"amount": amount,
						"currency": currency,
					}
				),
			}
		).insert(ignore_permissions=True)
		return refund.id


def cancel_open_intent(doc, method=None):
	"""A cancelled document can't be paid: its PaymentIntent is cancelled at Stripe once the
	cancel is saved."""
	request = intent_request(doc.doctype, doc.name)
	if request and request.status not in ("Completed", "Cancelled"):
		frappe.enqueue(
			"payments.payment_gateways.doctype.stripe_settings.stripe_settings.cancel_intent",
			integration_request=request.name,
			enqueue_after_commit=True,
		)


def cancel_paid_intents(doc, method=None):
	"""Payment Entry on_submit: a Payment Request it pays in full can't be paid again by card."""
	for name in {row.payment_request for row in doc.get("references") or [] if row.get("payment_request")}:
		if frappe.db.get_value("Payment Request", name, "status") == "Paid":
			cancel_open_intent(frappe._dict(doctype="Payment Request", name=name))


def cancel_intent(integration_request: str) -> None:
	request = frappe.get_doc("Integration Request", integration_request)
	gateway = frappe.db.get_value(request.reference_doctype, request.reference_docname, "payment_gateway")
	controller = frappe.db.get_value("Payment Gateway", gateway, "gateway_controller") if gateway else None
	if not controller or not frappe.db.exists("Stripe Settings", controller):
		return
	client = frappe.get_doc("Stripe Settings", controller).client()
	intent = client.payment_intents.retrieve(request.request_id)
	if intent.status in ("canceled", "succeeded"):
		return
	client.payment_intents.cancel(intent.id)
	request.db_set("status", "Cancelled", update_modified=False)


def intent_request(reference_doctype: str, reference_docname: str):
	"""The Integration Request of the latest PaymentIntent made for a document."""
	return frappe.db.get_value(
		"Integration Request",
		{
			"integration_request_service": "Stripe",
			"reference_doctype": reference_doctype,
			"reference_docname": reference_docname,
			"request_id": ["like", "pi_%"],
		},
		["name", "request_id", "status"],
		as_dict=True,
		order_by="creation desc",
	)


# Stripe calls this; the event's signature is checked against the gateway's webhook secret.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def webhook(gateway: str | None = None):
	"""Record payments Stripe reports with payment_intent.succeeded, whether or not the payer
	came back to the site."""
	import stripe

	# Stripe posts JSON, so the gateway named in the webhook's URL is read from the URL
	gateway = gateway or frappe.request.args.get("gateway")
	if not gateway or not frappe.db.exists("Stripe Settings", gateway):
		raise frappe.PermissionError
	settings = frappe.get_doc("Stripe Settings", gateway)
	secret = settings.get_password(fieldname="webhook_secret", raise_exception=False)
	if not secret:
		raise frappe.PermissionError
	try:
		event = stripe.Webhook.construct_event(
			frappe.request.get_data(), frappe.get_request_header("Stripe-Signature"), secret
		)
	except (ValueError, stripe.SignatureVerificationError):
		raise frappe.PermissionError
	if event.type == "payment_intent.succeeded":
		# the PaymentIntent is fetched from Stripe rather than taken from the event
		intent = settings.client().payment_intents.retrieve(event.data.object.id)
		settings.record_payment_intent(intent)
		# Stripe retries until the payment is stored, and webhooks are not committed otherwise
		frappe.db.commit()  # nosemgrep
	elif event.type in ("charge.dispute.created", "charge.dispute.closed"):
		record_dispute(settings.client().disputes.retrieve(event.data.object.id))
		frappe.db.commit()  # nosemgrep
	elif event.type in ("refund.created", "refund.updated", "refund.failed"):
		record_refund(settings.client().refunds.retrieve(event.data.object.id))
		frappe.db.commit()  # nosemgrep
	return {"received": True}


def record_refund(refund) -> None:
	"""Tell staff, on the documents the payment paid, about a refund made in Stripe's dashboard once
	it has gone through, and about any refund that failed. A refund made here is logged here."""
	if not refund.payment_intent or refund.status not in ("succeeded", "failed", "canceled"):
		return
	# the log is written in the transaction of the document refunding: if that was undone, the
	# refund carries its key but is recorded nowhere
	key = (refund.metadata or {}).get("key")
	if refund.status == "succeeded" and frappe.db.exists(
		"Integration Request", {"integration_request_service": "Stripe", "request_id": refund.id}
	):
		return
	paid = paid_request(refund.payment_intent)
	if not paid or not first_time("Stripe", f"{refund.id}:{refund.status}", {"refund": refund.id}):
		return
	reference = frappe.get_doc(paid.reference_doctype, paid.reference_docname)
	amount = fmt_money(major_units(refund.amount, refund.currency), currency=refund.currency.upper())
	if refund.status == "succeeded" and key:
		note = _(
			"{0} of the card payment for {1} {2} was refunded for {3}, but the refund wasn't recorded here. Check that it is recorded in ERPNext, and don't refund it again."
		).format(amount, _(reference.doctype), reference.name, key)
	elif refund.status == "succeeded":
		note = _(
			"{0} of the card payment for {1} {2} was refunded in Stripe. Record the refund in ERPNext, and don't refund it again."
		).format(amount, _(reference.doctype), reference.name)
	else:
		note = _(
			"A refund of {0} of the card payment for {1} {2} failed in Stripe: the money wasn't returned to the customer. Refund it again, or another way."
		).format(amount, _(reference.doctype), reference.name)
	tell_staff(reference, note)


def paid_request(payment_intent: str | None):
	"""The document a PaymentIntent paid, when it still exists."""
	if not payment_intent:
		return None
	paid = frappe.db.get_value(
		"Integration Request",
		{"integration_request_service": "Stripe", "request_id": payment_intent},
		["reference_doctype", "reference_docname"],
		as_dict=True,
	)
	if not paid or not frappe.db.exists(paid.reference_doctype, paid.reference_docname):
		return None
	return paid


def record_dispute(dispute) -> None:
	"""Tell staff about a payment the customer disputed with their bank (a chargeback), on the
	documents it paid. Disputes are answered in Stripe; what was lost is recorded by staff."""
	paid = paid_request(dispute.payment_intent)
	if not paid or not first_time("Stripe", f"{dispute.id}:{dispute.status}", {"dispute": dispute.id}):
		return
	amount = fmt_money(major_units(dispute.amount, dispute.currency), currency=dispute.currency.upper())
	reason = (dispute.reason or "general").replace("_", " ")
	if dispute.status in ("won", "warning_closed"):
		note = _("The dispute over {0} was closed in your favour: the payment stands.").format(amount)
	elif dispute.status == "lost":
		note = _(
			"The customer won their dispute over {0}: Stripe has taken the money back, with its dispute fee. Record the loss in ERPNext, and don't refund it again."
		).format(amount)
	else:
		due = (dispute.get("evidence_details") or {}).get("due_by")
		note = _(
			"The customer disputed {0} with their bank ({1}). Answer it in Stripe by {2}, or the money goes back to them."
		).format(
			amount,
			reason,
			format_datetime(
				convert_utc_to_system_timezone(datetime.fromtimestamp(due, UTC).replace(tzinfo=None))
			)
			if due
			else _("the date Stripe gives"),
		)
	tell_staff(frappe.get_doc(paid.reference_doctype, paid.reference_docname), note)


def success_location(reference_doctype: str, reference_docname: str) -> str:
	return f"/payment-success?{urlencode({'doctype': reference_doctype, 'docname': reference_docname})}"


def not_recorded_location(cancelled: bool = False) -> str:
	"""Where a payer goes when their payment came in but couldn't be recorded."""
	if cancelled:
		return frappe.redirect_to_message(
			_("Payment received after cancellation"),
			_("Your payment came in after this was cancelled. We've told our team, and they'll refund it."),
		)
	return frappe.redirect_to_message(
		_("Payment received"),
		_(
			"Your payment came in, but it couldn't be matched to what you owe. We've told our team, and they'll sort it out."
		),
	)


def get_gateway_controller(doctype, docname, payment_gateway=None):
	"""The Stripe Settings a document is paid through. A Payment Request is always paid through its
	own gateway, whatever gateway its payer names."""
	if doctype == "Payment Request" or not payment_gateway:
		payment_gateway = frappe.db.get_value(doctype, docname, "payment_gateway")
	if not payment_gateway:
		return None
	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")
