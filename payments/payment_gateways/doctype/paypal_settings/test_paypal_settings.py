# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.payment_gateways.doctype.paypal_settings import paypal_settings as pp


class Response:
	def __init__(self, status_code=200, body=None):
		self.status_code = status_code
		self.body = body or {}
		self.content = json.dumps(self.body).encode()

	def json(self):
		return self.body


class FakePayPal:
	"""PayPal's REST API, answering from routes {(method, path): body, Response or callable}."""

	def __init__(self, routes=None):
		self.routes = dict(routes or {})
		self.calls = []

	def __call__(self, method, url, **kwargs):
		path = url.split(".paypal.com", 1)[1]
		self.calls.append(frappe._dict(kwargs, method=method, url=url, path=path))
		if path == "/v1/oauth2/token" and (method, path) not in self.routes:
			return Response(200, {"access_token": f"token-{len(self.calls)}", "expires_in": 32400})
		answer = self.routes[(method, path)]
		if callable(answer):
			answer = answer(**kwargs)
		return answer if isinstance(answer, Response) else Response(200, answer)

	def made(self, method, path):
		return [call for call in self.calls if (call.method, call.path) == (method, path)]


def no_network(method, url, **kwargs):
	raise AssertionError(f"PayPal was called in a test: {method} {url}")


def paypal_order(order_id, status="APPROVED", value="300.00", currency="USD", capture=None):
	unit = {"reference_id": "PR", "amount": {"currency_code": currency, "value": value}}
	if capture:
		unit["payments"] = {"captures": [capture]}
	links = [{"rel": "self", "href": f"https://api-m.sandbox.paypal.com/v2/checkout/orders/{order_id}"}]
	if status in ("CREATED", "PAYER_ACTION_REQUIRED"):
		# as PayPal answers: an approved order has no link for the payer
		links.append(
			{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={order_id}"}
		)
	return {
		"id": order_id,
		"status": status,
		"purchase_units": [unit],
		"payer": {"email_address": "payer@example.com"},
		"links": links,
	}


def captured(order_id, value="300.00", currency="USD", status="COMPLETED", unit_value=None):
	return paypal_order(
		order_id,
		"COMPLETED",
		value=unit_value or value,
		currency=currency,
		capture={
			"id": f"CAP-{order_id}",
			"status": status,
			"amount": {"currency_code": currency, "value": value},
		},
	)


def payment_request(
	name,
	docstatus=1,
	status="Requested",
	grand_total=500,
	outstanding=300,
	currency="USD",
	email_to="payer@example.com",
):
	values = {
		"status": status,
		"grand_total": grand_total,
		"outstanding_amount": outstanding,
		"currency": currency,
		"email_to": email_to,
	}
	request = MagicMock(docstatus=docstatus, outstanding_amount=outstanding)
	request.doctype, request.name = "Payment Request", name
	request.get.side_effect = lambda field, default=None: values.get(field, default)
	request.meta.has_field.return_value = True
	request.run_method.return_value = None
	return request


class TestPayPalSettings(IntegrationTestCase):
	def setUp(self):
		self.started = frappe.utils.now_datetime()
		self.client_ids = []
		self.reference_name = f"_Test PayPal PR-{frappe.generate_hash(length=8)}"
		guard = patch.object(pp, "http_request", no_network)
		guard.start()
		self.addCleanup(guard.stop)

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.delete(
			"Integration Request",
			{"integration_request_service": "PayPal", "creation": [">=", self.started]},
		)
		frappe.db.commit()  # nosemgrep
		for client_id in self.client_ids:
			for host in (pp.SANDBOX_HOST, pp.LIVE_HOST):
				frappe.cache.delete_value(pp.token_cache_key(host, client_id))
		super().tearDown()

	def settings(self, **values):
		settings = frappe.new_doc("PayPal Settings")
		client_id = f"_test-client-{frappe.generate_hash(length=8)}"
		self.client_ids.append(client_id)
		settings.update(
			{
				"client_id": client_id,
				"client_secret": "_test-secret",
				"paypal_sandbox": 1,
				"webhook_id": "_TEST-WEBHOOK",
				**values,
			}
		)
		return settings

	def order_request(self, order_id=None, status="Queued", data=None, output=None):
		request = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "PayPal",
				"request_description": pp.ORDER,
				"status": status,
				"reference_doctype": "Payment Request",
				"reference_docname": self.reference_name,
				"request_id": order_id,
				"data": frappe.as_json(data or {"amount": 0.5, "currency": "USD"}),
				"output": frappe.as_json(output) if output else None,
			}
		)
		request.flags.ignore_links = True
		request.insert(ignore_permissions=True)
		return request

	def test_sandbox_and_live_hosts(self):
		settings = self.settings(paypal_sandbox=1)
		self.assertTrue(settings.test_mode)
		fake = FakePayPal({("GET", "/v2/checkout/orders/O-1"): {"id": "O-1"}})
		with patch.object(pp, "http_request", fake):
			settings.api("GET", "/v2/checkout/orders/O-1")
			self.assertTrue(
				all(call.url.startswith("https://api-m.sandbox.paypal.com/") for call in fake.calls)
			)
			settings.paypal_sandbox = 0
			self.assertFalse(settings.test_mode)
			fake.calls.clear()
			settings.api("GET", "/v2/checkout/orders/O-1")
			self.assertTrue(all(call.url.startswith("https://api-m.paypal.com/") for call in fake.calls))

	def test_access_token_is_cached_and_renewed_when_refused(self):
		settings = self.settings()
		answers = []
		fake = FakePayPal(
			{
				("GET", "/v2/checkout/orders/O-1"): lambda **kwargs: answers.pop(0)
				if answers
				else {"id": "O-1"}
			}
		)
		with patch.object(pp, "http_request", fake):
			settings.api("GET", "/v2/checkout/orders/O-1")
			settings.api("GET", "/v2/checkout/orders/O-1")
			tokens = fake.made("POST", "/v1/oauth2/token")
			self.assertEqual(len(tokens), 1)
			self.assertEqual(tokens[0].auth, (settings.client_id, "_test-secret"))
			self.assertEqual(tokens[0].data, {"grant_type": "client_credentials"})
			cached = frappe.cache.get_value(
				pp.token_cache_key(pp.SANDBOX_HOST, settings.client_id), expires=True
			)
			self.assertEqual(
				fake.made("GET", "/v2/checkout/orders/O-1")[1].headers["Authorization"], f"Bearer {cached}"
			)

			# PayPal no longer takes the cached token: a new one is fetched and the call made again
			answers.append(Response(401, {"error": "invalid_token"}))
			self.assertEqual(settings.api("GET", "/v2/checkout/orders/O-1"), {"id": "O-1"})
			self.assertEqual(len(fake.made("POST", "/v1/oauth2/token")), 2)
			calls = fake.made("GET", "/v2/checkout/orders/O-1")
			self.assertNotEqual(calls[-1].headers["Authorization"], calls[-2].headers["Authorization"])

			fake.routes[("POST", "/v1/oauth2/token")] = Response(401, {"error": "invalid_client"})
			with self.assertRaises(pp.PayPalError):
				settings.access_token(fresh=True)
			self.assertRaises(frappe.ValidationError, settings.validate_rest_credentials)

	def test_amounts_have_the_currencys_decimals(self):
		self.assertEqual(pp.paypal_amount(1000.4, "jpy"), {"currency_code": "JPY", "value": "1000"})
		self.assertEqual(pp.paypal_amount(2500.5, "HUF")["value"], "2501")
		self.assertEqual(pp.paypal_amount(99, "TWD")["value"], "99")
		self.assertEqual(pp.paypal_amount(12.5, "USD")["value"], "12.50")
		self.assertEqual(pp.paypal_amount(0.005, "USD")["value"], "0.01")
		self.assertTrue(
			pp.same_amount(
				{"currency_code": "JPY", "value": "1000"}, {"currency_code": "JPY", "value": "1000.00"}
			)
		)

		settings = self.settings()
		settings.validate_transaction_currency("CNY")
		self.assertRaises(frappe.ValidationError, settings.validate_transaction_currency, "RUB")
		classic = self.settings(client_id=None, client_secret=None, api_username="classic")
		classic.validate_transaction_currency("RUB")

	def test_classic_api_is_kept_for_recurring_payments_and_sites_without_rest_credentials(self):
		classic = self.settings(client_id=None, client_secret=None, api_username="classic")
		with patch.object(pp.PayPalSettings, "get_classic_payment_url", return_value="classic") as get_url:
			self.assertEqual(classic.get_payment_url(amount=10, currency="USD"), "classic")
			rest = self.settings(api_username="classic")
			self.assertEqual(
				rest.get_payment_url(
					amount=10, currency="USD", subscription_details={"billing_period": "Month"}
				),
				"classic",
			)
			self.assertEqual(get_url.call_count, 2)
			with self.assertRaises(frappe.ValidationError):
				self.settings().get_payment_url(amount=10, currency="USD", subscription_details={"x": 1})

	def test_payment_url_is_on_the_site_and_reused(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "_Test PayPal"}).insert(
			ignore_permissions=True
		)
		settings = self.settings()
		details = {
			"amount": 10,
			"currency": "USD",
			"reference_doctype": "ToDo",
			"reference_docname": todo.name,
		}
		url = settings.get_payment_url(**details)
		self.assertIn("paypal_settings.pay?integration_request=", url)
		self.assertEqual(settings.get_payment_url(**details), url)

		# any other document with a total is paid through its Payment Request, which records it
		order = MagicMock(doctype="Sales Order")
		with patch.object(pp, "get_reference", return_value=order), self.assertRaises(frappe.ValidationError):
			settings.get_payment_url(
				amount=10, currency="USD", reference_doctype="Sales Order", reference_docname="SO-1"
			)

	def test_a_link_is_made_while_erpnext_submits_the_payment_request(self):
		# ERPNext asks for the payment URL in the Payment Request's before_submit
		settings = self.settings()
		from frappe.model.document import Document

		draft = payment_request("_Test PR Submitting", docstatus=0, outstanding=0)
		with (
			patch.object(pp, "get_reference", return_value=draft),
			# the draft is in the database by then; this one is made up
			patch.object(Document, "_validate_links", lambda doc: None),
		):
			url = settings.get_payment_url(
				amount=300,
				currency="USD",
				reference_doctype="Payment Request",
				reference_docname="_Test PR Submitting",
			)
		self.assertIn("paypal_settings.pay?integration_request=", url)
		request = frappe.get_doc("Integration Request", url.rsplit("=", 1)[1])
		# what the payer is asked for is worked out when the link is opened
		with patch.object(pp, "get_reference", return_value=payment_request("_Test PR Submitting")):
			self.assertEqual(settings.amount_due(request)["value"], "300.00")

	def test_order_is_for_what_the_payment_request_still_asks(self):
		settings = self.settings()
		request = self.order_request(
			data={
				"amount": 0.5,
				"currency": "USD",
				"title": "_Test Company",
				# ERPNext names the user who made the request when it has no email
				"payer_email": "staff@example.com",
			}
		)
		reference = payment_request(self.reference_name, grand_total=500, outstanding=300)
		fake = FakePayPal({("POST", "/v2/checkout/orders"): paypal_order("ORDER-1", "PAYER_ACTION_REQUIRED")})
		with (
			patch.object(pp, "http_request", fake),
			patch.object(pp, "get_reference", return_value=reference),
		):
			location = settings.checkout(request)
			self.assertEqual(location, "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-1")
			created = fake.made("POST", "/v2/checkout/orders")
			body = created[0].json
			unit = body["purchase_units"][0]
			self.assertEqual(body["intent"], "CAPTURE")
			self.assertEqual(unit["amount"], {"currency_code": "USD", "value": "300.00"})
			self.assertEqual(
				(unit["custom_id"], unit["invoice_id"]), (self.reference_name, self.reference_name)
			)
			experience = body["payment_source"]["paypal"]["experience_context"]
			self.assertIn(f"complete_payment?integration_request={request.name}", experience["return_url"])
			self.assertIn(f"cancel_payment?integration_request={request.name}", experience["cancel_url"])
			self.assertEqual(body["payment_source"]["paypal"]["email_address"], "payer@example.com")
			self.assertTrue(created[0].headers["PayPal-Request-Id"])
			request.reload()
			self.assertEqual(request.request_id, "ORDER-1")

			# coming back to pay, the order is reused while it is for what is asked
			fake.routes[("GET", "/v2/checkout/orders/ORDER-1")] = paypal_order(
				"ORDER-1", "PAYER_ACTION_REQUIRED"
			)
			settings.checkout(request)
			self.assertEqual(len(fake.made("POST", "/v2/checkout/orders")), 1)

			reference.outstanding_amount = 200
			settings.checkout(request)
			created = fake.made("POST", "/v2/checkout/orders")
			self.assertEqual(len(created), 2)
			self.assertEqual(created[1].json["purchase_units"][0]["amount"]["value"], "200.00")
			self.assertNotEqual(
				created[0].headers["PayPal-Request-Id"], created[1].headers["PayPal-Request-Id"]
			)

			paid = payment_request(self.reference_name, status="Paid")
			with patch.object(pp, "get_reference", return_value=paid), self.assertRaises(pp.NotPayable):
				settings.checkout(request)

	def test_a_payment_is_captured_and_recorded_once(self):
		settings = self.settings()
		request = self.order_request(order_id="ORDER-2")
		stale = pp.order_request(name=request.name)
		reference = payment_request(self.reference_name)
		state = {"order": paypal_order("ORDER-2", "APPROVED")}

		def capture(**kwargs):
			state["order"] = captured("ORDER-2")
			# PayPal's answer to a capture leaves out what the order was for
			answer = json.loads(json.dumps(state["order"]))
			del answer["purchase_units"][0]["amount"]
			return answer

		fake = FakePayPal(
			{
				("GET", "/v2/checkout/orders/ORDER-2"): lambda **kwargs: state["order"],
				("POST", "/v2/checkout/orders/ORDER-2/capture"): capture,
			}
		)
		with (
			patch.object(pp, "http_request", fake),
			patch.object(pp, "get_reference", return_value=reference),
		):
			location = settings.complete(pp.order_request(name=request.name))
			self.assertTrue(location.startswith("/payment-success?"))
			# PayPal's webhooks for the same payment, and the payer reloading the page
			settings.handle_event(
				{
					"event_type": "PAYMENT.CAPTURE.COMPLETED",
					"resource": {
						"id": "CAP-ORDER-2",
						"supplementary_data": {"related_ids": {"order_id": "ORDER-2"}},
					},
				}
			)
			settings.handle_event({"event_type": "CHECKOUT.ORDER.APPROVED", "resource": {"id": "ORDER-2"}})
			settings.complete(pp.order_request(name=request.name))
			# read before the payment was recorded, as by a request arriving at the same moment
			self.assertTrue(settings.record_order(stale, state["order"]).startswith("/payment-success?"))

		reference.run_method.assert_called_once_with("on_payment_authorized", "Completed")
		captures = fake.made("POST", "/v2/checkout/orders/ORDER-2/capture")
		self.assertEqual(len(captures), 1)
		self.assertTrue(captures[0].headers["PayPal-Request-Id"])
		request.reload()
		self.assertEqual(request.status, "Completed")
		self.assertEqual(json.loads(request.output)["capture"], "CAP-ORDER-2")

	def test_the_payer_coming_back_is_sent_on(self):
		request = self.order_request(
			order_id="ORDER-3",
			status="Completed",
			output={"capture": "CAP-3", "redirect_to": "/store/order-confirmation/SO-3"},
		)
		settings = self.settings()
		reference = payment_request(self.reference_name, status="Paid")
		get_doc = frappe.get_doc
		with (
			patch(
				"frappe.get_doc",
				side_effect=lambda *args, **kwargs: settings
				if args[0] == "PayPal Settings"
				else get_doc(*args, **kwargs),
			),
			patch.object(pp, "get_reference", return_value=reference),
		):
			pp.complete_payment(integration_request=request.name)
		self.assertEqual(frappe.local.response["type"], "redirect")
		self.assertIn("/payment-success?", frappe.local.response["location"])
		# on to where the document sent the payer after paying, without recording it again
		self.assertIn("order-confirmation", frappe.local.response["location"])
		reference.run_method.assert_not_called()

		pp.complete_payment(integration_request="_Test PayPal missing")
		self.assertIn("/message?id=", frappe.local.response["location"])
		pp.cancel_payment(integration_request="_Test PayPal missing")
		self.assertIn("/message?id=", frappe.local.response["location"])

	def test_a_declined_payer_chooses_another_way_to_pay(self):
		settings = self.settings()
		request = self.order_request(order_id="ORDER-5")
		reference = payment_request(self.reference_name)
		declined = Response(
			422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "INSTRUMENT_DECLINED"}]}
		)
		fake = FakePayPal(
			{
				("GET", "/v2/checkout/orders/ORDER-5"): paypal_order("ORDER-5", "APPROVED"),
				("POST", "/v2/checkout/orders/ORDER-5/capture"): declined,
			}
		)
		with (
			patch.object(pp, "http_request", fake),
			patch.object(pp, "get_reference", return_value=reference),
		):
			location = settings.complete(request)
			self.assertEqual(location, "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-5")
			fake.routes[("POST", "/v2/checkout/orders/ORDER-5/capture")] = captured("ORDER-5")
			settings.complete(pp.order_request(name=request.name))
		keys = [
			call.headers["PayPal-Request-Id"]
			for call in fake.made("POST", "/v2/checkout/orders/ORDER-5/capture")
		]
		self.assertEqual(len(set(keys)), 2)
		reference.run_method.assert_called_once()

	def test_a_payment_that_doesnt_match_isnt_recorded(self):
		settings = self.settings()
		reference = payment_request(self.reference_name, grand_total=500)
		for order in (
			captured("ORDER-6", value="100.00", unit_value="300.00"),
			captured("ORDER-7", value="600.00"),
		):
			request = self.order_request(order_id=order["id"])
			fake = FakePayPal({("GET", f"/v2/checkout/orders/{order['id']}"): order})
			with (
				patch.object(pp, "http_request", fake),
				patch.object(pp, "get_reference", return_value=reference),
				patch.object(pp, "tell_staff") as tell_staff,
				patch("frappe.log_error"),
			):
				self.assertIsInstance(settings.complete(request), pp.Message)
			request.reload()
			self.assertEqual(request.status, "Failed")
			tell_staff.assert_called_once()
		reference.run_method.assert_not_called()

	def test_a_second_order_for_one_request_isnt_recorded_past_what_is_owed(self):
		settings = self.settings()
		# the other order paid it all, or all but 100
		for order, reference in (
			(captured("ORDER-8"), payment_request(self.reference_name, status="Paid", outstanding=0)),
			(
				captured("ORDER-9"),
				payment_request(self.reference_name, status="Partially Paid", outstanding=100),
			),
		):
			request = self.order_request(order_id=order["id"])
			fake = FakePayPal({("GET", f"/v2/checkout/orders/{order['id']}"): order})
			with (
				patch.object(pp, "http_request", fake),
				patch.object(pp, "get_reference", return_value=reference) as get_reference,
				patch.object(pp, "tell_staff") as tell_staff,
				patch("frappe.log_error"),
			):
				self.assertIsInstance(settings.complete(request), pp.Message)
			# read with the lock the other order's recording holds until it is done
			self.assertTrue(get_reference.call_args.kwargs.get("for_update"))
			request.reload()
			self.assertEqual(request.status, "Failed")
			tell_staff.assert_called_once()
			reference.run_method.assert_not_called()

	def test_a_cancelled_payment_request_isnt_charged(self):
		settings = self.settings()
		request = self.order_request(order_id="ORDER-8")
		reference = payment_request(self.reference_name, docstatus=2, status="Cancelled")
		fake = FakePayPal({("GET", "/v2/checkout/orders/ORDER-8"): paypal_order("ORDER-8", "APPROVED")})
		with (
			patch.object(pp, "http_request", fake),
			patch.object(pp, "get_reference", return_value=reference),
		):
			message = settings.complete(request)
		self.assertIsInstance(message, pp.Message)
		# said to the payer, without the document's name
		self.assertNotIn(self.reference_name, str(message))
		self.assertIn("haven't been charged", str(message))
		self.assertFalse(fake.made("POST", "/v2/checkout/orders/ORDER-8/capture"))
		request.reload()
		self.assertEqual(request.status, "Cancelled")
		reference.run_method.assert_not_called()

	def test_a_payment_after_cancellation_is_kept_for_staff(self):
		settings = self.settings()
		request = self.order_request(order_id="ORDER-9")
		reference = payment_request(self.reference_name, docstatus=2, status="Cancelled")
		fake = FakePayPal({("GET", "/v2/checkout/orders/ORDER-9"): captured("ORDER-9", value="12.50")})
		with (
			patch.object(pp, "http_request", fake),
			patch.object(pp, "get_reference", return_value=reference),
			patch.object(pp, "tell_staff") as tell_staff,
			patch("frappe.log_error") as log_error,
		):
			self.assertIsInstance(settings.complete(request), pp.Message)
		request.reload()
		self.assertEqual(request.status, "Failed")
		reference.run_method.assert_not_called()
		log_error.assert_called_once()
		self.assertIn("12.50", tell_staff.call_args.args[1])

	def test_webhook_needs_paypals_word_for_its_signature(self):
		settings = self.settings(webhook_id=None)
		paid = self.order_request(order_id="ORDER-10", status="Completed")
		event_id = f"WH-{frappe.generate_hash(length=8)}"
		body = json.dumps(
			{
				"id": event_id,
				"event_type": "PAYMENT.CAPTURE.COMPLETED",
				"resource": {"id": "CAP-10", "supplementary_data": {"related_ids": {"order_id": "ORDER-10"}}},
			}
		)
		http = MagicMock()
		http.get_data.return_value = body
		http.headers = {header: "x" for header in pp.SIGNATURE_HEADERS.values()}
		http.headers["PAYPAL-CERT-URL"] = "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1"
		verdict = {"verification_status": "FAILURE"}
		fake = FakePayPal({("POST", "/v1/notifications/verify-webhook-signature"): lambda **kwargs: verdict})
		get_doc = frappe.get_doc
		with (
			patch.object(frappe.local, "request", http, create=True),
			patch.object(frappe.local, "request_ip", "_test-paypal-webhook", create=True),
			patch.object(pp, "http_request", fake),
			patch(
				"frappe.get_doc",
				side_effect=lambda *args, **kwargs: settings
				if args[0] == "PayPal Settings"
				else get_doc(*args, **kwargs),
			),
		):
			# without a Webhook ID nothing is taken as from PayPal
			self.assertRaises(frappe.PermissionError, pp.webhook)
			self.assertFalse(fake.calls)
			settings.webhook_id = "_TEST-WEBHOOK"
			self.assertRaises(frappe.PermissionError, pp.webhook)
			sent = fake.made("POST", "/v1/notifications/verify-webhook-signature")[0].data
			self.assertIn('"webhook_id": "_TEST-WEBHOOK"', sent)
			# the event goes back to PayPal exactly as it came
			self.assertTrue(sent.endswith(f'"webhook_event": {body}}}'))
			self.assertEqual(json.loads(sent)["transmission_sig"], "x")

			headers = http.headers
			http.headers = {}
			self.assertRaises(frappe.PermissionError, pp.webhook)
			# a certificate that isn't PayPal's is refused without asking PayPal
			for cert_url in (
				"https://example.com/certs/CERT-1",
				"https://api.paypal.com.example.com/certs/CERT-1",
				"http://api.paypal.com/v1/notifications/certs/CERT-1",
			):
				http.headers = {**headers, "PAYPAL-CERT-URL": cert_url}
				self.assertRaises(frappe.PermissionError, pp.webhook)
			self.assertEqual(len(fake.made("POST", "/v1/notifications/verify-webhook-signature")), 1)
			http.headers = headers

			verdict["verification_status"] = "SUCCESS"
			self.assertEqual(pp.webhook(), {"received": True})
			self.assertTrue(
				frappe.db.exists(
					"Integration Request", {"request_id": event_id, "reference_docname": paid.name}
				)
			)
			# delivered again, the event is acted on once, even when both copies come at once
			with patch.object(pp.PayPalSettings, "handle_event") as handle_event:
				self.assertEqual(pp.webhook(), {"received": True})
				with patch.object(pp.frappe.db, "exists", return_value=False):
					self.assertEqual(pp.webhook(), {"received": True})
			handle_event.assert_not_called()

	def test_refund_takes_back_part_of_a_payment_once(self):
		settings = self.settings()
		paid = self.order_request(
			order_id="ORDER-11",
			status="Completed",
			output={"order": "ORDER-11", "capture": "CAP-11", "currency": "USD", "amount": 300},
		)
		refunds = []

		def refund(**kwargs):
			made = {
				"id": f"REF-{kwargs['json']['custom_id'].removeprefix(pp.REFUND_MARK)}",
				"status": "COMPLETED",
				"amount": kwargs["json"]["amount"],
				"custom_id": kwargs["json"]["custom_id"],
			}
			refunds.append(made)
			return made

		order = paypal_order("ORDER-11", "COMPLETED")
		order["purchase_units"][0]["payments"] = {"refunds": refunds}
		fake = FakePayPal(
			{
				("GET", "/v2/checkout/orders/ORDER-11"): order,
				("POST", "/v2/payments/captures/CAP-11/refund"): refund,
			}
		)
		with patch.object(pp, "http_request", fake):
			self.assertEqual(
				settings.refund("Payment Request", self.reference_name, 12.5, "CN-1"), "REF-CN-1"
			)
			made = fake.made("POST", "/v2/payments/captures/CAP-11/refund")
			self.assertEqual(
				made[0].json,
				{"amount": {"currency_code": "USD", "value": "12.50"}, "custom_id": "refund:CN-1"},
			)
			self.assertTrue(made[0].headers["PayPal-Request-Id"])
			self.assertTrue(
				frappe.db.exists(
					"Integration Request",
					{
						"request_id": "REF-CN-1",
						"reference_doctype": "Integration Request",
						"reference_docname": paid.name,
					},
				)
			)
			# asked again, the refund already made for that credit note is the answer
			self.assertEqual(
				settings.refund("Payment Request", self.reference_name, 12.5, "CN-1"), "REF-CN-1"
			)
			self.assertEqual(len(fake.made("POST", "/v2/payments/captures/CAP-11/refund")), 1)
			# its log undone with the credit note, and asked again for another amount: PayPal's
			# refund for that credit note is the answer, and is logged again
			frappe.db.delete("Integration Request", {"request_id": "REF-CN-1"})
			self.assertEqual(settings.refund("Payment Request", self.reference_name, 13, "CN-1"), "REF-CN-1")
			self.assertEqual(len(fake.made("POST", "/v2/payments/captures/CAP-11/refund")), 1)
			logged = frappe.db.get_value("Integration Request", {"request_id": "REF-CN-1"}, "data")
			self.assertEqual(json.loads(logged)["amount"], 12.5)

			self.assertEqual(settings.refund("Payment Request", self.reference_name, 5, "CN-2"), "REF-CN-2")
			made = fake.made("POST", "/v2/payments/captures/CAP-11/refund")
			self.assertEqual(made[1].json["amount"]["value"], "5.00")
			self.assertNotEqual(made[0].headers["PayPal-Request-Id"], made[1].headers["PayPal-Request-Id"])

			with self.assertRaises(frappe.ValidationError):
				settings.refund("Payment Request", "_Test PayPal PR-unpaid", 5, "CN-3")
			with self.assertRaises(frappe.ValidationError):
				settings.refund("Payment Request", self.reference_name, 0, "CN-5")
			frappe.set_user("Guest")
			try:
				with self.assertRaises(frappe.PermissionError):
					settings.refund("Payment Request", self.reference_name, 5, "CN-6")
			finally:
				frappe.set_user("Administrator")

			# part was refunded in PayPal: no more than what is left is refunded here
			refunds.append({"id": "REF-PAYPAL", "status": "COMPLETED", "amount": {"value": "250.00"}})
			with self.assertRaises(frappe.ValidationError) as refused:
				settings.refund("Payment Request", self.reference_name, 50, "CN-4")
			self.assertIn("32.50", str(refused.exception))
			self.assertEqual(len(fake.made("POST", "/v2/payments/captures/CAP-11/refund")), 2)
			self.assertEqual(
				settings.refund("Payment Request", self.reference_name, 32.5, "CN-4"), "REF-CN-4"
			)

	def test_refunds_made_in_paypal_reversals_and_disputes_are_noted_for_staff(self):
		settings = self.settings()
		self.order_request(order_id="ORDER-12", status="Completed", output={"capture": "CAP-12"})
		reference = payment_request(self.reference_name)
		fake = FakePayPal(
			{
				("GET", "/v2/payments/captures/CAP-12"): {
					"id": "CAP-12",
					"supplementary_data": {"related_ids": {"order_id": "ORDER-12"}},
				}
			}
		)
		refund = {
			"id": "REF-X",
			# as PayPal sends it: a refund made in PayPal carries the payment's custom_id
			"custom_id": self.reference_name,
			"amount": {"currency_code": "USD", "value": "20.00"},
			"links": [{"rel": "up", "href": "https://api-m.sandbox.paypal.com/v2/payments/captures/CAP-12"}],
		}
		dispute = {
			"dispute_id": "PP-D-1",
			"disputed_transactions": [{"seller_transaction_id": "CAP-12"}],
			"reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
			"dispute_amount": {"currency_code": "USD", "value": "300.00"},
			"seller_response_due_date": "2026-11-20T23:59:59.000Z",
		}
		with (
			patch.object(pp, "http_request", fake),
			patch.object(pp, "get_reference", return_value=reference),
			patch.object(pp, "tell_staff") as tell_staff,
		):
			settings.handle_event({"event_type": "PAYMENT.CAPTURE.REFUNDED", "resource": refund})
			self.assertIn("20.00", tell_staff.call_args.args[1])
			self.assertIn("refunded in PayPal", tell_staff.call_args.args[1])

			# a refund made here is recorded here, even when its key is the document's name and its
			# news comes before it is logged
			tell_staff.reset_mock()
			for key in ("CN-1", self.reference_name):
				settings.handle_event(
					{
						"event_type": "PAYMENT.CAPTURE.REFUNDED",
						"resource": dict(refund, id=f"REF-{key}", custom_id=pp.refund_mark(key)),
					}
				)
			tell_staff.assert_not_called()

			settings.handle_event({"event_type": "PAYMENT.CAPTURE.REVERSED", "resource": refund})
			self.assertIn("took back", tell_staff.call_args.args[1])

			settings.handle_event({"event_type": "CUSTOMER.DISPUTE.CREATED", "resource": dispute})
			note = tell_staff.call_args.args[1]
			self.assertIn("300.00", note)
			self.assertIn("merchandise or service not received", note)

			resolved = dict(dispute, dispute_outcome={"outcome_code": "RESOLVED_BUYER_FAVOUR"})
			settings.handle_event({"event_type": "CUSTOMER.DISPUTE.RESOLVED", "resource": resolved})
			self.assertIn("won their PayPal dispute", tell_staff.call_args.args[1])
			resolved = dict(dispute, dispute_outcome={"outcome_code": "RESOLVED_SELLER_FAVOUR"})
			settings.handle_event({"event_type": "CUSTOMER.DISPUTE.RESOLVED", "resource": resolved})
			self.assertIn("in your favour", tell_staff.call_args.args[1])
		reference.run_method.assert_not_called()
		self.assertFalse([call for call in fake.calls if call.method == "POST" and "refund" in call.path])

	def test_a_payment_request_can_be_paid_again_after_an_attempt_that_took_nothing(self):
		settings = self.settings()
		declined = self.order_request(
			order_id="ORDER-20", status="Failed", output={"capture": "CAP-20", "status": "DECLINED"}
		)
		reference = payment_request(self.reference_name)
		fake = FakePayPal(
			{("POST", "/v2/checkout/orders"): paypal_order("ORDER-21", "PAYER_ACTION_REQUIRED")}
		)
		asked = []

		def order_request_for(details):
			asked.append(details)
			return self.order_request().name

		with (
			patch.object(pp, "http_request", fake),
			patch.object(pp, "get_reference", return_value=reference),
			patch.object(settings, "order_request_for", side_effect=order_request_for),
		):
			location = settings.checkout(pp.order_request(name=declined.name))
		self.assertEqual(
			(asked[0]["reference_doctype"], asked[0]["reference_docname"]),
			("Payment Request", self.reference_name),
		)
		self.assertEqual(location, "https://www.sandbox.paypal.com/checkoutnow?token=ORDER-21")
		fresh = frappe.db.get_value(
			"Integration Request",
			{"request_id": "ORDER-21", "integration_request_service": "PayPal"},
			["name", "status"],
			as_dict=True,
		)
		self.assertEqual(fresh.status, "Queued")
		self.assertNotEqual(fresh.name, declined.name)

		# money taken that couldn't be recorded is left for staff, not asked for again
		kept = self.order_request(
			order_id="ORDER-22", status="Failed", output={"capture": "CAP-22", "status": "COMPLETED"}
		)
		with (
			patch.object(pp, "http_request", FakePayPal()),
			patch.object(pp, "get_reference", return_value=reference),
		):
			self.assertEqual(settings.checkout(pp.order_request(name=kept.name)), "/payment-failed")
