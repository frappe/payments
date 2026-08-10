# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
import razorpay
from frappe.tests import IntegrationTestCase, UnitTestCase

from payments.payment_gateways.doctype.razorpay_settings.razorpay_settings import (
	RazorpaySettings,
	from_paise,
	handle_refund_notification,
	process_webhook,
	razorpay_webhook,
	to_paise,
)


class TestRazorpayMoney(UnitTestCase):
	def test_to_paise_converts_major_units_to_integer_paise(self):
		self.assertEqual(to_paise(100), 10000)
		self.assertEqual(to_paise(99.99), 9999)

	def test_to_paise_rounds_instead_of_truncating(self):
		# 8.35 * 100 is 834.9999999999999, and truncating refunds a paise short.
		self.assertEqual(to_paise(8.35), 835)

	def test_from_paise_converts_integer_paise_to_major_units(self):
		self.assertEqual(from_paise(10000), 100.0)
		self.assertEqual(from_paise(9999), 99.99)


class TestRazorpayFetch(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.get_single("Razorpay Settings")

	def test_fetch_payment_delegates_to_sdk(self):
		client = MagicMock()
		client.payment.fetch.return_value = {"id": "pay_123", "status": "captured"}

		with patch.object(RazorpaySettings, "get_client", return_value=client):
			payment = self.settings.fetch_payment("pay_123")

		client.payment.fetch.assert_called_once_with("pay_123")
		self.assertEqual(payment["status"], "captured")

	def test_fetch_refund_delegates_to_sdk(self):
		client = MagicMock()
		client.refund.fetch.return_value = {"id": "rfnd_123", "status": "processed"}

		with patch.object(RazorpaySettings, "get_client", return_value=client):
			refund = self.settings.fetch_refund("rfnd_123")

		client.refund.fetch.assert_called_once_with("rfnd_123")
		self.assertEqual(refund["status"], "processed")


CAPTURED_PAYMENT = {
	"id": "pay_123",
	"status": "captured",
	"amount": 10000,
	"amount_refunded": 4000,
	"currency": "INR",
}


class TestRazorpayRefund(IntegrationTestCase):
	def setUp(self):
		self.settings = frappe.get_single("Razorpay Settings")

	def test_refund_rejects_payment_that_is_not_captured(self):
		payment = dict(CAPTURED_PAYMENT, status="authorized")

		with patch.object(RazorpaySettings, "fetch_payment", return_value=payment):
			self.assertRaises(frappe.ValidationError, self.settings.refund_payment, "pay_123")

	def test_refund_rejects_amount_above_refundable_balance(self):
		with patch.object(RazorpaySettings, "fetch_payment", return_value=CAPTURED_PAYMENT):
			self.assertRaises(frappe.ValidationError, self.settings.refund_payment, "pay_123", 60.01)

	def test_refund_rejects_zero_and_negative_amounts(self):
		with patch.object(RazorpaySettings, "fetch_payment", return_value=CAPTURED_PAYMENT):
			self.assertRaises(frappe.ValidationError, self.settings.refund_payment, "pay_123", 0)
			self.assertRaises(frappe.ValidationError, self.settings.refund_payment, "pay_123", -5)

	def test_refund_defaults_to_the_full_refundable_balance(self):
		client = MagicMock()
		client.payment.refund.return_value = {"id": "rfnd_1", "status": "processed", "amount": 6000}

		with (
			patch.object(RazorpaySettings, "fetch_payment", return_value=CAPTURED_PAYMENT),
			patch.object(RazorpaySettings, "get_client", return_value=client),
		):
			refund = self.settings.refund_payment("pay_123")

		client.payment.refund.assert_called_once_with("pay_123", 6000)
		self.assertEqual(refund["id"], "rfnd_1")

	def test_refund_sends_partial_amount_as_paise(self):
		client = MagicMock()
		client.payment.refund.return_value = {"id": "rfnd_2", "status": "pending", "amount": 835}

		with (
			patch.object(RazorpaySettings, "fetch_payment", return_value=CAPTURED_PAYMENT),
			patch.object(RazorpaySettings, "get_client", return_value=client),
		):
			self.settings.refund_payment("pay_123", 8.35)

		client.payment.refund.assert_called_once_with("pay_123", 835)

	def test_refund_translates_sdk_rejection_into_a_user_facing_error(self):
		client = MagicMock()
		client.payment.refund.side_effect = razorpay.errors.BadRequestError(
			"The amount must be atleast INR 1.00"
		)

		with (
			patch.object(RazorpaySettings, "fetch_payment", return_value=CAPTURED_PAYMENT),
			patch.object(RazorpaySettings, "get_client", return_value=client),
		):
			with self.assertRaises(frappe.ValidationError) as raised:
				self.settings.refund_payment("pay_123", 10)

		self.assertIn("atleast INR 1.00", str(raised.exception))


REFUND_PROCESSED_PAYLOAD = {
	"event": "refund.processed",
	"payload": {
		"refund": {
			"entity": {
				"id": "rfnd_1",
				"status": "processed",
				"amount": 6000,
				"payment_id": "pay_123",
			}
		}
	},
}


def sign(body: bytes, secret: str) -> str:
	return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestRazorpayWebhook(IntegrationTestCase):
	def setUp(self):
		self.secret = "whsec_test"
		self.body = json.dumps(REFUND_PROCESSED_PAYLOAD).encode()
		self.patcher = patch.object(RazorpaySettings, "get_password", return_value=self.secret)
		self.patcher.start()
		self.addCleanup(self.patcher.stop)

	def test_rejects_a_bad_signature(self):
		self.assertRaises(frappe.PermissionError, process_webhook, self.body, "deadbeef")

	def test_rejects_a_webhook_when_no_secret_is_configured(self):
		# Anyone can compute an HMAC under an empty key, forged payloads included.
		self.patcher.stop()
		for unset in (None, ""):
			with self.subTest(secret=unset):
				with patch.object(RazorpaySettings, "get_password", return_value=unset):
					self.assertRaises(
						frappe.ValidationError, process_webhook, self.body, sign(self.body, unset or "")
					)
		self.patcher.start()

	def test_ignores_unsupported_events(self):
		body = json.dumps(dict(REFUND_PROCESSED_PAYLOAD, event="payment.captured")).encode()

		self.assertIsNone(process_webhook(body, sign(body, self.secret)))

	def test_logs_a_supported_event_as_an_integration_request(self):
		name = process_webhook(self.body, sign(self.body, self.secret))

		log = frappe.get_doc("Integration Request", name)
		self.assertEqual(log.status, "Queued")
		self.assertEqual(json.loads(log.data)["event"], "refund.processed")

	def test_a_rejected_webhook_logs_nothing(self):
		before = frappe.db.count("Integration Request")

		self.assertRaises(frappe.PermissionError, process_webhook, self.body, "deadbeef")

		self.assertEqual(frappe.db.count("Integration Request"), before)


class TestRazorpayWebhookEndpoint(IntegrationTestCase):
	def setUp(self):
		self.secret = "whsec_test"
		self.body = json.dumps(REFUND_PROCESSED_PAYLOAD).encode()
		patcher = patch.object(RazorpaySettings, "get_password", return_value=self.secret)
		patcher.start()
		self.addCleanup(patcher.stop)

	def post(self, signature):
		request = MagicMock()
		request.data = self.body

		with (
			patch.object(frappe, "request", request),
			patch.object(frappe, "get_request_header", return_value=signature),
			patch.object(frappe, "enqueue") as enqueue,
		):
			return razorpay_webhook(), enqueue

	def test_a_bad_signature_is_answered_normally_and_left_in_the_error_log(self):
		integration_requests = frappe.db.count("Integration Request")
		error_logs = frappe.db.count("Error Log")

		response, enqueue = self.post("deadbeef")

		self.assertIsNone(response)
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.count("Integration Request"), integration_requests)
		self.assertEqual(frappe.db.count("Error Log"), error_logs + 1)

	def test_a_valid_webhook_is_queued_for_processing(self):
		_, enqueue = self.post(sign(self.body, self.secret))

		self.assertEqual(enqueue.call_args.kwargs["doctype"], "Integration Request")
		self.assertEqual(
			frappe.db.get_value("Integration Request", enqueue.call_args.kwargs["docname"], "status"),
			"Queued",
		)


class TestRazorpayRefundNotification(IntegrationTestCase):
	def setUp(self):
		self.log = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Razorpay",
				"request_description": "Refund Notification",
				"data": json.dumps(REFUND_PROCESSED_PAYLOAD),
				"is_remote_request": 1,
				"status": "Queued",
			}
		).insert(ignore_permissions=True)

	def test_a_handled_notification_completes_the_request(self):
		with patch(
			"payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.call_hook_method"
		) as hook:
			handle_refund_notification("Integration Request", self.log.name)

		hook.assert_called_once_with(
			"handle_refund_notification", doctype="Integration Request", docname=self.log.name
		)
		self.assertEqual(frappe.db.get_value("Integration Request", self.log.name, "status"), "Completed")

	def test_a_subscriber_failure_is_rolled_back_and_recorded_on_the_request(self):
		with (
			patch(
				"payments.payment_gateways.doctype.razorpay_settings.razorpay_settings.call_hook_method",
				side_effect=Exception("subscriber blew up"),
			),
			patch.object(frappe.db, "rollback") as rollback,
		):
			handle_refund_notification("Integration Request", self.log.name)

		rollback.assert_called_once()
		status, error = frappe.db.get_value("Integration Request", self.log.name, ["status", "error"])
		self.assertEqual(status, "Failed")
		self.assertIn("subscriber blew up", error)
