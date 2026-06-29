# Copyright (c) 2017, Frappe Technologies and contributors
# License: MIT. See LICENSE

import hmac
from types import MappingProxyType
from urllib.parse import quote, urlencode

import frappe
import stripe
from frappe import _
from frappe.integrations.utils import create_request_log, make_get_request
from frappe.model.document import Document
from frappe.utils import call_hook_method, flt, get_url

from payments.payment_gateways.stripe_utils import (
	get_or_create_customer,
	get_stripe_client,
	idempotency_key,
	to_minor_units,
)
from payments.utils import create_payment_gateway

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
				# PaymentIntents is the current API; /v1/charges is the deprecated one.
				make_get_request(url="https://api.stripe.com/v1/payment_intents?limit=1", headers=header)
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
		if (self.checkout_mode or "Hosted Checkout") == "Hosted Checkout":
			return self.create_checkout_session(kwargs)
		# Embedded Elements: render the on-site card form (PaymentElement).
		return get_url(f"./stripe_checkout?{urlencode(kwargs)}")

	def get_stripe_metadata(self, data=None, integration_request=None):
		"""Stripe metadata is string->string; drop empty values."""
		data = data or self.data
		meta = {
			"reference_doctype": data.get("reference_doctype"),
			"reference_docname": data.get("reference_docname"),
			"integration_request": integration_request,
		}
		return {k: str(v) for k, v in meta.items() if v is not None}

	def get_party_for_reference(self, data):
		"""Resolve the ERPNext Customer behind a payment's reference document."""
		dt, dn = data.get("reference_doctype"), data.get("reference_docname")
		if not dt or not dn or not frappe.db.exists(dt, dn):
			return None
		if dt == "Payment Request":
			row = frappe.db.get_value("Payment Request", dn, ["party_type", "party"], as_dict=True)
			if row and row.party_type == "Customer":
				return row.party
			return None
		if frappe.get_meta(dt).has_field("customer"):
			return frappe.db.get_value(dt, dn, "customer")
		return None

	def resolve_stripe_customer(self, client, data):
		"""Reusable Stripe customer id for this payment's party (saves the card for reuse)."""
		party = self.get_party_for_reference(data)
		return get_or_create_customer(
			client,
			customer=party,
			email=data.get("payer_email"),
			name=data.get("payer_name") or party,
		)

	def create_setup_intent_for_card(self, data):
		"""SetupIntent to save a card off-session without charging (for later reuse)."""
		data = frappe._dict(data)
		client = get_stripe_client(self)
		customer_id = self.resolve_stripe_customer(client, data)
		intent = client.setup_intents.create(
			{
				"customer": customer_id,
				"usage": "off_session",
				"metadata": self.get_stripe_metadata(data=data),
			}
		)
		return {"client_secret": intent.client_secret, "setup_intent": intent.id, "customer": customer_id}

	def create_request(self, data):
		self.data = frappe._dict(data)
		self.stripe = get_stripe_client(self)

		try:
			if self.data.get("payment_intent"):
				# Embedded flow already confirmed client-side; reuse the stamped Integration Request.
				return self.finalize_payment_intent_by_id(self.data.get("payment_intent"))

			self.integration_request = create_request_log(self.data, service_name="Stripe")
			return self.create_payment_intent_on_stripe()

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

	def create_payment_intent_for_checkout(self, data):
		"""Embedded Elements: create an unconfirmed PaymentIntent and return its secret.

		Idempotent per reference: a page reload or network retry reuses the open
		PaymentIntent instead of orphaning Integration Requests and abandoning PIs.
		"""
		client = get_stripe_client(self)

		reused = self._reuse_open_checkout_intent(client, data)
		if reused:
			return reused

		integration_request = create_request_log(data, service_name="Stripe")
		customer_id = self.resolve_stripe_customer(client, data)
		intent = client.payment_intents.create(
			{
				"amount": to_minor_units(data.amount, data.currency),
				"currency": (data.currency or "").lower(),
				"description": data.get("description"),
				"receipt_email": data.get("payer_email"),
				"customer": customer_id,
				"metadata": self.get_stripe_metadata(data=data, integration_request=integration_request.name),
				"automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
			}
		)
		integration_request.db_set("output", intent.id, update_modified=False)
		return {"client_secret": intent.client_secret, "payment_intent": intent.id}

	def _reuse_open_checkout_intent(self, client, data):
		"""Return an existing open PaymentIntent for this reference, if still reusable.

		A reload/retry hits create_payment_intent_for_checkout again; without this
		each call mints a fresh PaymentIntent + Integration Request, leaving orphans.
		Only an unconfirmed intent for the same amount is reused.
		"""
		reference_doctype = data.get("reference_doctype")
		reference_docname = data.get("reference_docname")
		if not (reference_doctype and reference_docname):
			return None
		rows = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Stripe",
				"status": "Queued",
				"reference_doctype": reference_doctype,
				"reference_docname": reference_docname,
			},
			fields=["output"],
			order_by="creation desc",
			limit=1,
		)
		if not rows or not rows[0].output:
			return None
		try:
			intent = client.payment_intents.retrieve(rows[0].output)
		except Exception:
			return None
		if intent.get("status") not in ("requires_payment_method", "requires_confirmation"):
			return None
		if intent.get("amount") != to_minor_units(data.amount, data.currency):
			return None
		return {"client_secret": intent.client_secret, "payment_intent": intent.id}

	def create_payment_intent_on_stripe(self):
		"""Confirm a one-off payment via PaymentIntents (SCA/3DS ready)."""
		client = self.stripe
		try:
			payment_method = self.data.get("payment_method")
			if not payment_method and self.data.get("stripe_token_id"):
				# Backward-compat: convert a legacy card token to a PaymentMethod.
				payment_method = client.payment_methods.create(
					{"type": "card", "card": {"token": self.data.get("stripe_token_id")}}
				).id
			customer_id = self.resolve_stripe_customer(client, self.data)
			params = {
				"amount": to_minor_units(self.data.amount, self.data.currency),
				"currency": (self.data.currency or "").lower(),
				"payment_method": payment_method,
				"confirm": bool(payment_method),
				"description": self.data.get("description"),
				"receipt_email": self.data.get("payer_email"),
				"customer": customer_id,
				"metadata": self.get_stripe_metadata(integration_request=self.integration_request.name),
				"automatic_payment_methods": {"enabled": True, "allow_redirects": "never"},
			}
			# Store the card for off-session reuse only with explicit consent.
			if self.data.get("save_card") and customer_id:
				params["setup_future_usage"] = "off_session"
			intent = client.payment_intents.create(
				params,
				# Idempotency key on the payment method dedupes a retry of the same attempt.
				{
					"idempotency_key": idempotency_key(
						"pi", self.data.get("reference_docname"), self.data.get("amount"), payment_method
					)
				},
			)
			return self.handle_payment_intent_status(intent)

		except stripe.error.CardError as e:
			self.integration_request.db_set("status", "Failed", update_modified=False)
			self.integration_request.db_set("error", frappe.as_json(e.json_body), update_modified=False)
			return {"redirect_to": "payment-failed", "status": "Failed", "error": e.user_message}

	def handle_payment_intent_status(self, intent):
		if intent.status == "succeeded":
			self.integration_request.db_set("status", "Completed", update_modified=False)
			self.integration_request.db_set("output", intent.id, update_modified=False)
			self.flags.status_changed_to = "Completed"
			return self.finalize_request()

		if intent.status in ("requires_action", "requires_confirmation"):
			# Card needs extra authentication (3DS) — let the client finish.
			return {
				"requires_action": True,
				"client_secret": intent.client_secret,
				"payment_intent": intent.id,
				"status": "Pending",
			}

		if intent.status == "processing":
			# Async method (e.g. bank transfer / delayed capture) still settling.
			# Leave the request pending — the payment_intent.succeeded webhook
			# finalizes it. Marking it Failed here would break these payments.
			return {"redirect_to": _success_redirect(dict(intent.get("metadata") or {})), "status": "Pending"}

		# requires_payment_method / canceled
		self.integration_request.db_set("status", "Failed", update_modified=False)
		return {"redirect_to": "payment-failed", "status": "Failed"}

	def assert_intent_matches_reference(self, intent):
		"""Bind a client-supplied PaymentIntent to the order being settled.

		Without this a caller could present a PaymentIntent that succeeded for a
		different (cheaper) order and settle this one for free. The reference in
		the intent's own metadata must match the reference we are about to settle.
		"""
		meta = dict(intent.get("metadata") or {})
		if (meta.get("reference_doctype"), meta.get("reference_docname")) != (
			self.data.get("reference_doctype"),
			self.data.get("reference_docname"),
		):
			frappe.throw(
				_("This payment does not belong to the order being settled."), frappe.PermissionError
			)

	def finalize_payment_intent_by_id(self, pi_id):
		"""Retrieve a (client-confirmed) PaymentIntent and finalize idempotently."""
		client = getattr(self, "stripe", None) or get_stripe_client(self)
		intent = client.payment_intents.retrieve(pi_id)
		# Reject a PaymentIntent minted for a different order before settling.
		self.assert_intent_matches_reference(intent)

		if intent.status == "succeeded":
			return self.finalize_payment_intent(intent)
		if intent.status in ("requires_action", "requires_confirmation"):
			return {
				"requires_action": True,
				"client_secret": intent.client_secret,
				"payment_intent": intent.id,
				"status": "Pending",
			}
		if intent.status == "processing":
			# Async method still settling; the payment_intent.succeeded webhook finalizes it.
			return {"redirect_to": _success_redirect(dict(intent.get("metadata") or {})), "status": "Pending"}
		return {"redirect_to": "payment-failed", "status": "Failed"}

	def finalize_payment_intent(self, intent, integration_request=None):
		"""Mark the original Integration Request complete and run on_payment_authorized.

		Idempotent: callable from the synchronous return AND the webhook. Keyed on
		the Integration Request stamped in the intent's metadata, so it runs once.
		"""
		metadata = dict(intent.get("metadata") or {})
		ir_name = integration_request or metadata.get("integration_request")
		if ir_name and frappe.db.exists("Integration Request", ir_name):
			self.integration_request = frappe.get_doc("Integration Request", ir_name)
		else:
			ir = frappe.db.get_value("Integration Request", {"output": intent.get("id")}, "name")
			self.integration_request = (
				frappe.get_doc("Integration Request", ir)
				if ir
				else create_request_log(intent, service_name="Stripe")
			)

		if self.integration_request.status == "Completed":
			return {"redirect_to": _success_redirect(metadata), "status": "Completed"}

		if not self.claim_integration_request():
			# Lost the race to a concurrent webhook/redirect — already settled.
			return {"redirect_to": _success_redirect(metadata), "status": "Completed"}

		self.data = frappe._dict(
			{
				"reference_doctype": metadata.get("reference_doctype"),
				"reference_docname": metadata.get("reference_docname"),
			}
		)
		self.integration_request.db_set("output", intent.get("id"), update_modified=False)
		self.flags.status_changed_to = "Completed"
		return self.finalize_request()

	def enable_setup_future_usage(self, payment_intent, client_secret, reference_doctype, reference_docname):
		"""Enable off-session reuse on an unconfirmed PaymentIntent (explicit consent)."""
		client = get_stripe_client(self)
		intent = client.payment_intents.retrieve(payment_intent)
		# Ownership proof: only the browser that created the intent holds its client_secret.
		if not client_secret or not hmac.compare_digest(intent.client_secret or "", client_secret):
			frappe.throw(_("Invalid payment session."), frappe.PermissionError)
		self.data = frappe._dict(
			{"reference_doctype": reference_doctype, "reference_docname": reference_docname}
		)
		self.assert_intent_matches_reference(intent)
		if intent.status not in ("requires_payment_method", "requires_confirmation"):
			return {"updated": False}
		if not intent.get("customer"):
			return {"updated": False}
		client.payment_intents.update(payment_intent, {"setup_future_usage": "off_session"})
		return {"updated": True}

	def create_checkout_session(self, data):
		"""Hosted Checkout: build a Stripe-hosted payment page and return its URL."""
		# Local import avoids a circular import (stripe_checkout imports this module).
		from payments.templates.pages.stripe_checkout import get_reference_amount, guard_payment_reference

		data = frappe._dict(data)
		# Same guard + server-side amount the embedded path uses: never trust a
		# client-supplied amount/currency or an unvalidated reference.
		guard_payment_reference(data.reference_doctype, data.reference_docname)
		data.amount, currency = get_reference_amount(data.reference_doctype, data.reference_docname)
		if currency:
			data.currency = currency
		client = get_stripe_client(self)
		integration_request = create_request_log(data, service_name="Stripe")

		success_url = get_url(
			"/api/method/payments.payment_gateways.doctype.stripe_settings.stripe_settings.checkout_success"
			+ "?session_id={CHECKOUT_SESSION_ID}&gateway="
			+ quote(self.name)
		)
		metadata = self.get_stripe_metadata(data=data, integration_request=integration_request.name)
		customer_id = self.resolve_stripe_customer(client, data)

		session = client.checkout.sessions.create(
			{
				"mode": "payment",
				"line_items": [
					{
						"price_data": {
							"currency": (data.currency or "").lower(),
							"unit_amount": to_minor_units(data.amount, data.currency),
							"product_data": {
								"name": data.get("description") or data.get("title") or _("Payment")
							},
						},
						"quantity": 1,
					}
				],
				"success_url": success_url,
				"cancel_url": get_url("payment-failed"),
				"client_reference_id": data.get("reference_docname"),
				"customer": customer_id,
				"payment_intent_data": {"metadata": metadata},
				"metadata": metadata,
				# Stripe renders its own opt-in checkbox; card saved only if the buyer ticks it.
				"saved_payment_method_options": {"payment_method_save": "enabled"},
			}
		)
		integration_request.db_set("output", session.id, update_modified=False)
		return session.url

	def claim_integration_request(self):
		"""Atomically claim the Integration Request for settlement.

		The Hosted Checkout return and the webhook can arrive concurrently. A
		SELECT ... FOR UPDATE serialises them: the first flips the status to
		Completed and settles; the second blocks, then sees Completed and backs
		off — so finalize_request() runs exactly once (no duplicate Payment Entry).
		Returns True only for the caller that won the claim.
		"""
		status = frappe.db.get_value(
			"Integration Request", self.integration_request.name, "status", for_update=True
		)
		if status == "Completed":
			return False
		self.integration_request.db_set("status", "Completed", update_modified=False)
		return True

	def finalize_checkout_session(self, session_id):
		"""Confirm a Hosted Checkout session and run on_payment_authorized.

		Idempotent: callable from both the success redirect and the webhook.
		"""
		client = get_stripe_client(self)
		session = client.checkout.sessions.retrieve(session_id)
		metadata = dict(session.get("metadata") or {})

		ir_name = metadata.get("integration_request")
		if ir_name and frappe.db.exists("Integration Request", ir_name):
			self.integration_request = frappe.get_doc("Integration Request", ir_name)
		else:
			ir = frappe.db.get_value("Integration Request", {"output": session_id}, "name")
			self.integration_request = (
				frappe.get_doc("Integration Request", ir)
				if ir
				else create_request_log(session, service_name="Stripe")
			)

		if self.integration_request.status == "Completed":
			return {"redirect_to": _success_redirect(metadata), "status": "Completed"}

		if session.get("payment_status") not in ("paid", "no_payment_required"):
			if session.get("status") == "complete":
				return {"redirect_to": _success_redirect(metadata), "status": "Pending"}
			return {"redirect_to": "payment-failed", "status": "Failed"}

		if not self.claim_integration_request():
			# Lost the race to a concurrent webhook/redirect — already settled.
			return {"redirect_to": _success_redirect(metadata), "status": "Completed"}

		self.data = frappe._dict(
			{
				"reference_doctype": metadata.get("reference_doctype"),
				"reference_docname": metadata.get("reference_docname"),
			}
		)
		self.integration_request.db_set("output", session.get("payment_intent"), update_modified=False)
		self.flags.status_changed_to = "Completed"
		return self.finalize_request()

	def create_charge_on_stripe(self):
		# Deprecated: the Charges API is replaced by PaymentIntents. Kept as a
		# thin shim so any external caller keeps working.
		return self.create_payment_intent_on_stripe()

	def authorize_reference(self):
		"""Settle the paid reference document.

		Custom doctypes that still define the legacy ``on_payment_authorized`` hook
		keep working. Newer ERPNext removed it from Payment Request, so a Payment
		Request is settled explicitly (status -> Paid + Payment Entry).
		"""
		ref = frappe.get_doc(self.data.reference_doctype, self.data.reference_docname)
		if hasattr(ref, "on_payment_authorized"):
			return ref.run_method("on_payment_authorized", self.flags.status_changed_to)
		if ref.doctype == "Payment Request":
			self.settle_payment_request(ref)
		return None

	def settle_payment_request(self, pr):
		"""Mark a submitted Payment Request paid and create its Payment Entry.

		Idempotent: skips if the request is already paid or a Payment Entry already
		exists for the underlying invoice. The Payment Entry's submission is what
		flips the Payment Request status to "Paid" (via ERPNext's PE -> PR sync).
		"""
		if pr.docstatus != 1 or pr.status == "Paid":
			return
		if pr.payment_channel == "Phone":
			pr.db_set({"status": "Paid", "outstanding_amount": 0})
			return

		from payments.utils import erpnext_app_import_guard

		with erpnext_app_import_guard():
			from erpnext.accounts.doctype.payment_request.payment_request import (
				get_existing_payment_entry,
			)

		if pr.reference_name and get_existing_payment_entry(pr.reference_name):
			return  # the invoice is already settled by a Payment Entry

		pr.set_as_paid()

	def finalize_request(self):
		redirect_to = self.data.get("redirect_to") or None
		redirect_message = self.data.get("redirect_message") or None
		status = self.integration_request.status
		redirect_url = "payment-success"

		if self.flags.status_changed_to == "Completed":
			if self.data.reference_doctype and self.data.reference_docname:
				custom_redirect_to = None
				try:
					custom_redirect_to = self.authorize_reference()
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
			redirect_url += "&" + urlencode({"redirect_message": redirect_message})

		return {"redirect_to": redirect_url, "status": status}


def get_gateway_controller(doctype, docname, payment_gateway=None):
	if not payment_gateway:
		reference_doc = frappe.get_doc(doctype, docname)
		payment_gateway = reference_doc.payment_gateway
	return frappe.db.get_value("Payment Gateway", payment_gateway, "gateway_controller")


def _success_redirect(metadata):
	"""payment-success URL carrying the reference, so the success page can load it."""
	dt = (metadata or {}).get("reference_doctype")
	dn = (metadata or {}).get("reference_docname")
	if dt and dn:
		return f"payment-success?{urlencode({'doctype': dt, 'docname': dn})}"
	return "payment-success"


@frappe.whitelist(allow_guest=True)
def checkout_success(session_id, gateway):
	"""Return landing for Hosted Checkout — verify the session, then redirect.

	The webhook (checkout.session.completed) is the authoritative backstop;
	finalize_checkout_session is idempotent so running both is safe.
	"""
	if not frappe.db.exists("Stripe Settings", gateway):
		frappe.local.response["type"] = "redirect"
		frappe.local.response["location"] = "/payment-failed"
		return

	result = {}
	try:
		settings = frappe.get_doc("Stripe Settings", gateway)
		result = settings.finalize_checkout_session(session_id) or {}
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Stripe checkout return failed")

	# Only land on the success page when settlement actually returned a result;
	# a swallowed exception leaves result empty, so fail visibly instead.
	redirect_url = result.get("redirect_to") or ("payment-success" if result else "payment-failed")
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = "/" + redirect_url.lstrip("/")
