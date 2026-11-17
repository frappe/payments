# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import frappe
from frappe.tests import IntegrationTestCase


class TestStripeSettings(IntegrationTestCase):
	def test_test_and_live_keys_cant_be_mixed(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import key_mode

		self.assertEqual(key_mode("pk_test_x"), "test")
		self.assertEqual(key_mode("sk_live_x"), "live")
		self.assertEqual(key_mode("rk_test_x"), "test")
		self.assertIsNone(key_mode(None))
		settings = frappe.new_doc("Stripe Settings")
		settings.update({"gateway_name": "Mixed", "publishable_key": "pk_test_x", "secret_key": "sk_live_x"})
		self.assertRaises(frappe.ValidationError, settings.validate)
		settings.secret_key = "sk_test_x"
		settings.validate()
		self.assertTrue(settings.test_mode)
		settings.publishable_key = "pk_live_x"
		settings.secret_key = "sk_live_x"
		settings.validate()
		self.assertFalse(settings.test_mode)

	def test_failed_payment_redirect_message_starts_query_string(self):
		controller = frappe.new_doc("Stripe Settings")
		controller.data = frappe._dict(redirect_message="Card declined")
		controller.integration_request = frappe._dict(status="Failed")

		response = controller.finalize_request()

		self.assertEqual(response["redirect_to"], "payment-failed?redirect_message=Card+declined")

	def test_failed_payment_redirect_message_follows_redirect_to(self):
		controller = frappe.new_doc("Stripe Settings")
		controller.data = frappe._dict(redirect_to="/orders", redirect_message="Card declined")
		controller.integration_request = frappe._dict(status="Failed")

		response = controller.finalize_request()

		self.assertEqual(
			response["redirect_to"],
			"payment-failed?redirect_to=%2Forders&redirect_message=Card+declined",
		)

	def test_amounts_are_in_the_currencys_smallest_unit(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import minor_units

		self.assertEqual(minor_units(293.29, "USD"), 29329)
		self.assertEqual(minor_units(1000, "JPY"), 1000)
		self.assertEqual(minor_units(12.35, "KWD"), 12350)

	def test_webhook_needs_a_gateway_with_a_signing_secret(self):
		from payments.payment_gateways.doctype.stripe_settings.stripe_settings import webhook

		with self.assertRaises(frappe.PermissionError):
			webhook(gateway="_Test Missing Gateway")
		settings = frappe.get_doc(
			{
				"doctype": "Stripe Settings",
				"gateway_name": "_Test Stripe",
				"publishable_key": "pk_test_x",
				"secret_key": "sk_test_x",
			}
		)
		settings.flags.ignore_mandatory = True
		settings.insert(ignore_permissions=True)
		with self.assertRaises(frappe.PermissionError):
			webhook(gateway="_Test Stripe")

	def test_refund_takes_back_part_of_a_card_payment_once(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		settings = frappe.new_doc("Stripe Settings")
		client = MagicMock()
		client.refunds.list.return_value = frappe._dict(data=[])
		client.refunds.create.return_value = frappe._dict(id="re_test_1")
		client.payment_intents.retrieve.return_value = frappe._dict(amount_received=5000)
		paid = frappe._dict(name="IR-1", request_id="pi_test_1", status="Completed")
		with (
			patch.object(stripe_settings.StripeSettings, "client", return_value=client),
			patch.object(stripe_settings, "intent_request", return_value=paid),
			patch("frappe.db.get_value", return_value="USD"),
			patch("frappe.get_doc") as log,
		):
			self.assertEqual(settings.refund("Payment Request", "PR-1", 12.5, "CN-1"), "re_test_1")
		params = client.refunds.create.call_args.kwargs["params"]
		self.assertEqual((params["payment_intent"], params["amount"]), ("pi_test_1", 1250))
		self.assertIn("CN-1", client.refunds.create.call_args.kwargs["options"]["idempotency_key"])
		logged = log.call_args.args[0]
		self.assertEqual(logged["request_id"], "re_test_1")
		self.assertEqual(
			(logged["reference_doctype"], logged["reference_docname"]), ("Integration Request", "IR-1")
		)
		log.return_value.insert.assert_called_once()
		with self.assertRaises(frappe.ValidationError):
			settings.refund("Payment Request", "PR-1", 0, "CN-0")
		with (
			patch.object(stripe_settings, "intent_request", return_value=None),
			self.assertRaises(frappe.ValidationError),
		):
			settings.refund("Payment Request", "PR-2", 5, "CN-2")

		# asked again days later, the refund already made for that credit note is the answer
		client.refunds.create.reset_mock()
		client.refunds.list.return_value = frappe._dict(
			data=[frappe._dict(id="re_test_1", status="succeeded", metadata={"key": "CN-1"})]
		)
		with (
			patch.object(stripe_settings.StripeSettings, "client", return_value=client),
			patch.object(stripe_settings, "intent_request", return_value=paid),
			patch("frappe.db.get_value", return_value="USD"),
		):
			self.assertEqual(settings.refund("Payment Request", "PR-1", 12.5, "CN-1"), "re_test_1")
		client.refunds.create.assert_not_called()

		# $40 of the $50 was refunded in Stripe's dashboard: no more than what is left is refunded here
		client.refunds.list.return_value = frappe._dict(
			data=[frappe._dict(id="re_dashboard", status="succeeded", amount=4000, metadata={})]
		)
		with (
			patch.object(stripe_settings.StripeSettings, "client", return_value=client),
			patch.object(stripe_settings, "intent_request", return_value=paid),
			patch("frappe.db.get_value", return_value="USD"),
			self.assertRaises(frappe.ValidationError) as refused,
		):
			settings.refund("Payment Request", "PR-1", 12.5, "CN-3")
		self.assertIn("10.00", str(refused.exception))
		client.refunds.create.assert_not_called()

	def test_a_refund_made_in_stripe_is_noted_for_staff(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		order = MagicMock(doctype="Payment Request")
		order.name = "PR-1"
		paid = frappe._dict(reference_doctype="Payment Request", reference_docname="PR-1")
		refund = frappe._dict(
			id="re_1", payment_intent="pi_1", amount=1000, currency="usd", status="succeeded", metadata={}
		)
		with (
			patch.object(stripe_settings, "paid_request", return_value=paid),
			patch.object(stripe_settings, "first_time", return_value=True) as first_time,
			patch("frappe.get_doc", return_value=order),
			patch.object(stripe_settings, "tell_staff") as tell_staff,
		):
			stripe_settings.record_refund(refund)
			self.assertIn("10.00", tell_staff.call_args.args[1])
			self.assertIn("refunded in Stripe", tell_staff.call_args.args[1])
			self.assertEqual(first_time.call_args.args[1], "re_1:succeeded")
			# a refund made here is logged here; one still pending is told once it is settled
			tell_staff.reset_mock()
			with patch("frappe.db.exists", return_value=True) as logged:
				stripe_settings.record_refund(frappe._dict(refund, metadata={"key": "CN-1"}))
			self.assertEqual(logged.call_args.args[1]["request_id"], "re_1")
			stripe_settings.record_refund(frappe._dict(refund, status="pending"))
			tell_staff.assert_not_called()
			# made here, but the document refunding was undone and the refund never logged
			with patch("frappe.db.exists", return_value=False):
				stripe_settings.record_refund(frappe._dict(refund, metadata={"key": "CN-1"}))
			self.assertIn("wasn't recorded here", tell_staff.call_args.args[1])
			self.assertIn("CN-1", tell_staff.call_args.args[1])
			tell_staff.reset_mock()
			# a refund that fails is told, wherever it was made
			stripe_settings.record_refund(frappe._dict(refund, status="failed", metadata={"key": "CN-1"}))
			self.assertIn("failed in Stripe", tell_staff.call_args.args[1])
			# two refunds of the same amount are two refunds
			tell_staff.reset_mock()
			stripe_settings.record_refund(frappe._dict(refund, id="re_2"))
			tell_staff.assert_called_once()
		with (
			patch.object(stripe_settings, "paid_request", return_value=paid),
			patch.object(stripe_settings, "first_time", return_value=False),
			patch.object(stripe_settings, "tell_staff") as tell_staff,
		):
			# the same news sent again
			stripe_settings.record_refund(refund)
		tell_staff.assert_not_called()
		# a refund of a payment taken without a PaymentIntent belongs to no document here
		self.assertIsNone(stripe_settings.paid_request(None))

	def test_pay_page_sends_a_payer_who_has_paid_on(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.templates.pages import stripe_checkout

		settings = MagicMock()
		settings.payment_intent.return_value = frappe._dict(status="succeeded", client_secret="cs_x")
		settings.record_payment_intent.return_value = frappe._dict(recorded=True, location="/order/SO-1")
		form = dict.fromkeys(stripe_checkout.expected_keys, "x")
		form.update(amount=10, currency="USD", reference_doctype="Payment Request")
		get_doc = frappe.get_doc
		with (
			patch.object(frappe.local, "form_dict", frappe._dict(form)),
			patch.object(stripe_checkout, "get_gateway_controller", return_value="_Test Stripe"),
			patch.object(stripe_checkout, "get_api_key", return_value="pk_test_x"),
			patch.object(stripe_checkout, "get_header_image", return_value=None),
			patch.object(stripe_checkout, "is_a_subscription", return_value=False),
			patch(
				"frappe.get_doc",
				side_effect=lambda *args, **kwargs: (
					settings if args[0] == "Stripe Settings" else get_doc(*args, **kwargs)
				),
			),
			self.assertRaises(frappe.Redirect),
		):
			stripe_checkout.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, "/order/SO-1")
		self.assertTrue(frappe.local.flags.commit)
		frappe.local.flags.commit = False

	def test_a_cancelled_payment_cant_be_paid(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.templates.pages import stripe_checkout

		settings = MagicMock()
		form = dict.fromkeys(stripe_checkout.expected_keys, "x")
		form.update(amount=10, currency="USD", reference_doctype="Payment Request")
		get_doc, get_value = frappe.get_doc, frappe.db.get_value
		with (
			patch.object(frappe.local, "form_dict", frappe._dict(form)),
			patch.object(stripe_checkout, "get_gateway_controller", return_value="_Test Stripe"),
			patch.object(stripe_checkout, "get_api_key", return_value="pk_test_x"),
			patch.object(stripe_checkout, "get_header_image", return_value=None),
			patch.object(stripe_checkout, "is_a_subscription", return_value=False),
			patch(
				"frappe.db.get_value",
				side_effect=lambda *args, **kwargs: (
					2 if args[:3] == ("Payment Request", "x", "docstatus") else get_value(*args, **kwargs)
				),
			),
			patch(
				"frappe.get_doc",
				side_effect=lambda *args, **kwargs: (
					settings if args[0] == "Stripe Settings" else get_doc(*args, **kwargs)
				),
			),
			self.assertRaises(frappe.Redirect),
		):
			stripe_checkout.get_context(frappe._dict())
		self.assertIn("/message", frappe.local.flags.redirect_location)
		settings.payment_intent.assert_not_called()

	def test_cancelling_a_document_cancels_its_open_intent(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		doc = frappe._dict(doctype="Payment Request", name="PR-1")
		open_request = frappe._dict(name="IR-1", request_id="pi_test_1", status="Queued")
		with (
			patch.object(stripe_settings, "intent_request", return_value=open_request),
			patch("frappe.enqueue") as enqueue,
		):
			stripe_settings.cancel_open_intent(doc)
		self.assertEqual(enqueue.call_args.kwargs["integration_request"], "IR-1")
		self.assertTrue(enqueue.call_args.kwargs["enqueue_after_commit"])
		with (
			patch.object(
				stripe_settings, "intent_request", return_value=frappe._dict(open_request, status="Completed")
			),
			patch("frappe.enqueue") as enqueue,
		):
			stripe_settings.cancel_open_intent(doc)
		enqueue.assert_not_called()

		request = MagicMock(
			reference_doctype="Payment Request", reference_docname="PR-1", request_id="pi_test_1"
		)
		client = MagicMock()
		gateway = MagicMock()
		gateway.client.return_value = client
		for status, cancelled in (("requires_payment_method", True), ("succeeded", False)):
			client.reset_mock()
			client.payment_intents.retrieve.return_value = frappe._dict(id="pi_test_1", status=status)
			with (
				patch(
					"frappe.get_doc",
					side_effect=lambda doctype, name: request
					if doctype == "Integration Request"
					else gateway,
				),
				patch("frappe.db.get_value", return_value="_Test Stripe"),
				patch("frappe.db.exists", return_value=True),
			):
				stripe_settings.cancel_intent("IR-1")
			self.assertEqual(client.payment_intents.cancel.called, cancelled)

	def record(self, intent=None, reference=None, status="Queued", output=None, controller="_Test Stripe"):
		"""Record a PaymentIntent for PR-1 with the Integration Request, document and gateway given."""
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		request = MagicMock(status=status)
		if reference is None:
			reference = MagicMock(docstatus=1, doctype="Payment Request")
			reference.name = "PR-1"
			reference.get.side_effect = {"payment_gateway": "Stripe-_Test Stripe"}.get
			reference.run_method.return_value = "/order/SO-1"
		intent = frappe._dict(
			{
				"id": "pi_test_1",
				"status": "succeeded",
				"amount": 1250,
				"amount_received": 1250,
				"currency": "usd",
				"livemode": False,
				"metadata": {
					"reference_doctype": "Payment Request",
					"reference_docname": "PR-1",
					"payment_gateway": "Stripe-_Test Stripe",
					"site": frappe.local.site,
				},
			}
			| (intent or {})
		)
		settings = frappe.new_doc("Stripe Settings")
		settings.name, settings.publishable_key = "_Test Stripe", "pk_test_x"
		with (
			patch(
				"frappe.db.get_value",
				return_value=frappe._dict(name="IR-1", status=status, output=output),
			),
			patch(
				"frappe.get_doc",
				side_effect=lambda doctype, name: request if doctype == "Integration Request" else reference,
			),
			patch.object(stripe_settings, "get_gateway_controller", return_value=controller),
			patch.object(stripe_settings, "amount_to_pay", return_value=(12.5, "USD")),
			patch("frappe.log_error"),
			patch.object(stripe_settings, "tell_staff") as tell_staff,
			patch("frappe.redirect_to_message", return_value="/message?id=1"),
		):
			outcome = settings.record_payment_intent(intent)
		return frappe._dict(outcome=outcome, request=request, reference=reference, tell_staff=tell_staff)

	def test_a_payment_is_recorded_once(self):
		import json

		first = self.record()
		self.assertEqual((first.outcome.recorded, first.outcome.location), (True, "/order/SO-1"))
		first.reference.run_method.assert_called_once_with("on_payment_authorized", "Completed")
		self.assertEqual(first.request.db_set.call_args_list[0].args, ("status", "Completed"))
		stored = first.request.db_set.call_args_list[1].args
		self.assertEqual((stored[0], json.loads(stored[1])), ("output", {"redirect_to": "/order/SO-1"}))
		# the payer comes back, or Stripe sends the event again: sent where they were sent, nothing more
		again = self.record(status="Completed", output=json.dumps({"redirect_to": "/order/SO-1"}))
		self.assertEqual((again.outcome.recorded, again.outcome.location), (True, "/order/SO-1"))
		again.reference.run_method.assert_not_called()

	def test_a_payment_that_cant_be_recorded_is_kept_for_staff(self):
		from unittest.mock import MagicMock

		cancelled = MagicMock(docstatus=2, doctype="Payment Request")
		cancelled.name = "PR-1"
		cancelled.get.side_effect = {"payment_gateway": "Stripe-_Test Stripe"}.get
		for case in (
			{"controller": "_Test Stripe Other"},
			{
				"intent": {
					"metadata": {
						"reference_doctype": "Payment Request",
						"reference_docname": "PR-1",
						"payment_gateway": "Stripe-Other",
						"site": frappe.local.site,
					}
				}
			},
			{"intent": {"livemode": True}},
			{"intent": {"amount": 2000, "amount_received": 2000}},
			{"intent": {"currency": "eur"}},
			{"reference": cancelled},
		):
			result = self.record(**case)
			self.assertFalse(result.outcome.recorded, case)
			self.assertEqual(result.outcome.location, "/message?id=1")
			self.assertEqual(result.request.db_set.call_args.args[0]["status"], "Failed")
			result.reference.run_method.assert_not_called()
			self.assertIn("12.50", result.tell_staff.call_args.args[1])
		# one already refused isn't told again
		again = self.record(status="Failed")
		self.assertFalse(again.outcome.recorded)
		again.tell_staff.assert_not_called()

	def test_nothing_left_to_pay_isnt_paid_again(self):
		from unittest.mock import patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		with patch.object(stripe_settings, "amount_to_pay", side_effect=frappe.ValidationError("paid")):
			settings = frappe.new_doc("Stripe Settings")
			settings.name, settings.publishable_key = "_Test Stripe", "pk_test_x"
			reference = frappe._dict(doctype="Sales Invoice", name="SINV-1", docstatus=1)
			intent = frappe._dict(
				livemode=False, metadata={}, currency="usd", amount=1250, amount_received=1250
			)
			self.assertIn("nothing left to pay", settings.payment_problem(intent, reference))

	def test_a_refused_payment_doesnt_block_paying_again(self):
		from unittest.mock import MagicMock, patch

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		reference = MagicMock()
		reference.get.return_value = ""
		settings = frappe.new_doc("Stripe Settings")
		settings.validate_transaction_currency = lambda currency: None
		settings.validate_minimum_transaction_amount = lambda currency, amount: None
		for status, expected in (("Queued", "pi_old"), ("Completed", "pi_old"), ("Failed", "pi_new")):
			client = MagicMock()
			client.payment_intents.retrieve.return_value = frappe._dict(id="pi_old", status="succeeded")
			client.payment_intents.create.return_value = frappe._dict(id="pi_new")
			settings.client = lambda client=client: client
			with (
				self.subTest(status=status),
				patch("frappe.get_doc", return_value=reference),
				patch.object(
					stripe_settings,
					"intent_request",
					return_value=frappe._dict(name="IR-1", request_id="pi_old", status=status),
				),
				patch.object(stripe_settings, "amount_to_pay", return_value=(12.5, "USD")),
				patch.object(stripe_settings, "create_request_log"),
				patch("frappe.db.count", return_value=1),
			):
				self.assertEqual(settings.payment_intent("Payment Request", "PR-1").id, expected)

	def test_a_payment_request_is_paid_through_its_own_gateway(self):
		from unittest.mock import patch

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		def get_value(doctype, name, field):
			return {
				("Payment Request", "PR-1", "payment_gateway"): "Stripe-Live",
				("Payment Gateway", "Stripe-Live", "gateway_controller"): "Live",
				("Payment Gateway", "Stripe-Test", "gateway_controller"): "Test",
			}.get((doctype, name, field))

		with patch("frappe.db.get_value", side_effect=get_value):
			# the payer can't choose another gateway, such as one in test mode
			self.assertEqual(
				stripe_settings.get_gateway_controller("Payment Request", "PR-1", "Stripe-Test"), "Live"
			)
			self.assertEqual(stripe_settings.get_gateway_controller("Payment Request", "PR-1"), "Live")
			self.assertEqual(
				stripe_settings.get_gateway_controller("Web Form", "WF-1", "Stripe-Test"), "Test"
			)

	def test_a_payment_that_wasnt_recorded_isnt_shown_as_a_success(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.templates.pages import stripe_checkout

		settings = MagicMock()
		settings.client.return_value.payment_intents.retrieve.return_value = frappe._dict(
			status="succeeded", metadata={"reference_doctype": "Payment Request", "reference_docname": "PR-1"}
		)
		settings.record_payment_intent.return_value = frappe._dict(recorded=False, location="/message?id=1")
		with (
			patch.object(stripe_checkout, "get_gateway_controller", return_value="_Test Stripe"),
			patch("frappe.get_doc", return_value=settings),
			patch("frappe.db.commit"),
		):
			result = stripe_checkout.confirm_payment("pi_1", "Payment Request", "PR-1", "Stripe-Other")
		self.assertEqual(result, {"status": "Not Recorded", "redirect_to": "/message?id=1"})

	def test_a_paid_request_cancels_its_open_intent(self):
		from unittest.mock import patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		entry = frappe._dict(
			references=[frappe._dict(payment_request="PR-1"), frappe._dict(payment_request=None)]
		)
		for status, cancelled in (("Paid", True), ("Partially Paid", False)):
			with (
				patch("frappe.db.get_value", return_value=status),
				patch.object(stripe_settings, "cancel_open_intent") as cancel,
			):
				stripe_settings.cancel_paid_intents(entry)
			self.assertEqual(cancel.called, cancelled)

	def test_refunds_are_a_staff_action(self):
		import frappe

		settings = frappe.new_doc("Stripe Settings")
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				settings.refund("Payment Request", "PR-1", 5, "CN-1")
		finally:
			frappe.set_user("Administrator")

	def test_a_request_without_the_payers_email_still_opens(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		settings = frappe.new_doc("Stripe Settings")
		client = MagicMock()
		client.payment_intents.create.return_value = frappe._dict(id="pi_test_2")
		request = frappe._dict(grand_total=40, currency="USD", email_to="Administrator", subject=None)
		with (
			patch.object(stripe_settings.StripeSettings, "client", return_value=client),
			patch.object(stripe_settings, "intent_request", return_value=None),
			patch("frappe.get_doc", return_value=request),
			patch.object(stripe_settings, "amount_to_pay", return_value=(40, "USD")),
			patch("frappe.db.count", return_value=0),
			patch.object(stripe_settings, "create_request_log"),
		):
			settings.payment_intent("Payment Request", "PR-3")
		self.assertIsNone(client.payment_intents.create.call_args.kwargs["params"]["receipt_email"])

	def test_without_a_webhook_only_cards_are_offered(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		request = frappe._dict(email_to="jane@example.com", subject="Payment Request for Jane Doe")
		for secret, offered in (
			(None, {"payment_method_types": ["card"]}),
			("whsec_x", {"automatic_payment_methods": {"enabled": True}}),
		):
			settings = frappe.new_doc("Stripe Settings")
			settings.get_password = lambda *args, secret=secret, **kwargs: secret
			client = MagicMock()
			client.payment_intents.create.return_value = frappe._dict(id="pi_test_3")
			with (
				patch.object(stripe_settings.StripeSettings, "client", return_value=client),
				patch.object(stripe_settings, "intent_request", return_value=None),
				patch("frappe.get_doc", return_value=request),
				patch.object(stripe_settings, "amount_to_pay", return_value=(40, "USD")),
				patch("frappe.db.count", return_value=0),
				patch.object(stripe_settings, "create_request_log"),
			):
				settings.payment_intent("Payment Request", "PR-4")
			params = client.payment_intents.create.call_args.kwargs["params"]
			self.assertEqual({key: params[key] for key in offered}, offered)
			self.assertEqual(len({"payment_method_types", "automatic_payment_methods"} & set(params)), 1)
			# the description, which the pay page shows, names the document and not the customer
			self.assertNotIn("Jane", params["description"])
			self.assertIn("PR-4", params["description"])

	def test_a_guessed_pay_link_isnt_shown_the_payment(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.templates.pages import stripe_checkout

		settings = MagicMock()
		settings.payment_intent.return_value = frappe._dict(
			status="requires_payment_method", client_secret="cs_x", currency="usd", amount=1000
		)
		get_doc, get_value = frappe.get_doc, frappe.db.get_value
		for payer_email, shown in (("", False), ("someone@example.com", False), ("Jane@Example.com", True)):
			form = dict.fromkeys(stripe_checkout.expected_keys, "x")
			form.update(
				amount=10, currency="USD", reference_doctype="Payment Request", payer_email=payer_email
			)
			context = frappe._dict()
			settings.reset_mock()
			with (
				patch.object(frappe.local, "form_dict", frappe._dict(form)),
				patch.object(stripe_checkout, "get_gateway_controller", return_value="_Test Stripe"),
				patch.object(stripe_checkout, "get_api_key", return_value="pk_test_x"),
				patch.object(stripe_checkout, "get_header_image", return_value=None),
				patch.object(stripe_checkout, "is_a_subscription", return_value=False),
				patch(
					"frappe.db.get_value",
					side_effect=lambda *args, **kwargs: (
						{"docstatus": 1, "email_to": "jane@example.com"}[args[2]]
						if args[:2] == ("Payment Request", "x")
						else get_value(*args, **kwargs)
					),
				),
				patch(
					"frappe.get_doc",
					side_effect=lambda *args, **kwargs: (
						settings if args[0] == "Stripe Settings" else get_doc(*args, **kwargs)
					),
				),
			):
				if shown:
					stripe_checkout.get_context(context)
				else:
					with self.assertRaises(frappe.Redirect):
						stripe_checkout.get_context(context)
			self.assertEqual(settings.payment_intent.called, shown)
			self.assertEqual(context.get("client_secret"), "cs_x" if shown else None)

	def test_the_old_card_form_posts_its_payment(self):
		import frappe

		from payments.templates.pages import stripe_checkout

		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[stripe_checkout.make_payment], ["POST"]
		)

	def test_a_disputed_payment_is_noted_for_staff(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings
		from payments.utils import payment_request

		order = MagicMock(doctype="Sales Order")
		order.name = "SO-1"
		request = MagicMock(doctype="Payment Request")
		request.name, request.reference_doctype, request.reference_name = "PR-1", "Sales Order", "SO-1"
		request.get.side_effect = {"reference_doctype": "Sales Order", "reference_name": "SO-1"}.get
		paid = frappe._dict(reference_doctype="Payment Request", reference_docname="PR-1")
		dispute = frappe._dict(
			id="dp_1",
			payment_intent="pi_1",
			amount=3800,
			currency="usd",
			reason="fraudulent",
			status="needs_response",
			evidence_details={"due_by": 1795000000},
		)

		docs = {"Payment Request": request, "Sales Order": order}
		real_get_doc, real_get_value, real_exists = frappe.get_doc, frappe.db.get_value, frappe.db.exists

		def get_value(doctype, *args, **kwargs):
			if doctype in ("Integration Request", "User"):
				return paid if doctype == "Integration Request" else 1
			return real_get_value(doctype, *args, **kwargs)

		def exists(doctype, *args, **kwargs):
			if doctype in docs or doctype == "Comment":
				return doctype != "Comment"
			return real_exists(doctype, *args, **kwargs)

		with (
			patch("frappe.db.get_value", side_effect=get_value),
			patch("frappe.db.exists", side_effect=exists),
			patch("frappe.get_doc", side_effect=lambda *a, **k: docs.get(a[0]) or real_get_doc(*a, **k)),
			patch.object(stripe_settings.frappe, "get_all", return_value=["accounts@example.com"]),
			patch.object(stripe_settings, "first_time", return_value=True),
			patch.object(payment_request, "enqueue_create_notification") as notify,
		):
			stripe_settings.record_dispute(dispute)
			note = order.add_comment.call_args.args[1]
			self.assertIn("38.00", note)
			self.assertIn("fraudulent", note)
			request.add_comment.assert_called_once()
			self.assertEqual(notify.call_args.args[0], ["accounts@example.com"])
			self.assertEqual(notify.call_args.args[1]["document_name"], "SO-1")

			stripe_settings.record_dispute(frappe._dict(dispute, status="lost"))
			self.assertIn("taken the money back", order.add_comment.call_args.args[1])

		# the same news sent again is noted, and told, once
		notified = notify.call_count
		with (
			patch("frappe.db.get_value", side_effect=get_value),
			patch("frappe.db.exists", return_value=True),
			patch("frappe.get_doc", side_effect=lambda *a, **k: docs.get(a[0]) or real_get_doc(*a, **k)),
			patch.object(stripe_settings, "first_time", return_value=False),
			patch.object(payment_request, "enqueue_create_notification") as notify_again,
		):
			stripe_settings.record_dispute(frappe._dict(dispute, status="lost"))
		notify_again.assert_not_called()
		self.assertTrue(notified)
		# a dispute over a payment taken without a PaymentIntent belongs to no document here
		with patch.object(stripe_settings, "tell_staff") as tell_staff:
			stripe_settings.record_dispute(frappe._dict(dispute, payment_intent=None))
		tell_staff.assert_not_called()

	def test_a_charge_is_for_what_the_document_still_asks(self):
		from unittest.mock import MagicMock, patch

		import frappe

		from payments.payment_gateways.doctype.stripe_settings import stripe_settings

		request = MagicMock(docstatus=1, status="Partially Paid", grand_total=500, outstanding_amount=300)
		request.doctype = "Payment Request"
		request.get.side_effect = lambda field, default=None: {
			"status": "Partially Paid",
			"grand_total": 500,
			"outstanding_amount": 300,
			"currency": "USD",
		}.get(field, default)
		request.meta.has_field.return_value = True
		self.assertEqual(stripe_settings.amount_to_pay(request), (300, "USD"))

		paid = MagicMock(docstatus=1)
		paid.get.side_effect = lambda field, default=None: {"status": "Paid"}.get(field, default)
		with self.assertRaises(frappe.ValidationError):
			stripe_settings.amount_to_pay(paid)

		# the old card form sends its own amount; the charge is for the document's instead
		settings = frappe.new_doc("Stripe Settings")
		settings.get_password = lambda *a, **k: "sk_test_x"
		with (
			patch("frappe.get_doc", return_value=request),
			patch.object(stripe_settings, "create_request_log"),
			patch.object(stripe_settings.StripeSettings, "create_charge_on_stripe", lambda s: s.data),
		):
			data = settings.create_request(
				{
					"amount": 0.5,
					"currency": "usd",
					"reference_doctype": "Payment Request",
					"reference_docname": "PR-9",
				}
			)
		self.assertEqual((data.amount, data.currency), (300, "USD"))

		# another document is charged as before, for what its payment page asks
		with (
			patch.object(stripe_settings, "create_request_log"),
			patch.object(stripe_settings.StripeSettings, "create_charge_on_stripe", lambda s: s.data),
		):
			data = settings.create_request(
				{
					"amount": 0.5,
					"currency": "usd",
					"reference_doctype": "Web Form",
					"reference_docname": "WF-1",
				}
			)
		self.assertEqual((data.amount, data.currency), (0.5, "usd"))
