# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import hashlib
import hmac
import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.payment_gateways.doctype.razorpay_settings.razorpay_settings import RazorpaySettings
from payments.payment_gateways.doctype.razorpay_settings.webhook import (
	handle_refund_notification,
	process_webhook,
	razorpay_webhook,
)

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

	def post(self, signature, event_id="evt_test"):
		request = MagicMock()
		request.data = self.body
		headers = {"X-Razorpay-Signature": signature, "X-Razorpay-Event-Id": event_id}

		with (
			patch.object(frappe, "request", request),
			patch.object(
				frappe,
				"get_request_header",
				side_effect=lambda key, default=None: headers.get(key, default),
			),
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

	def test_a_rejection_names_its_cause_and_the_delivery_it_came_from(self):
		self.post("deadbeef", event_id="evt_9f3c")

		log = frappe.get_last_doc("Error Log")
		self.assertIn("Signature Verification Failed", log.method)
		self.assertIn("evt_9f3c", log.error)

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
		with patch("payments.payment_gateways.doctype.razorpay_settings.webhook.call_hook_method") as hook:
			handle_refund_notification("Integration Request", self.log.name)

		hook.assert_called_once_with(
			"handle_refund_notification", doctype="Integration Request", docname=self.log.name
		)
		self.assertEqual(frappe.db.get_value("Integration Request", self.log.name, "status"), "Completed")

	def test_a_subscriber_failure_is_rolled_back_and_recorded_on_the_request(self):
		with (
			patch(
				"payments.payment_gateways.doctype.razorpay_settings.webhook.call_hook_method",
				side_effect=Exception("subscriber blew up"),
			),
			patch.object(frappe.db, "rollback") as rollback,
		):
			handle_refund_notification("Integration Request", self.log.name)

		rollback.assert_called_once()
		status, error = frappe.db.get_value("Integration Request", self.log.name, ["status", "error"])
		self.assertEqual(status, "Failed")
		self.assertIn("subscriber blew up", error)


class TestRazorpayWebhookQueueing(IntegrationTestCase):
	def setUp(self):
		self.secret = "whsec_test"
		self.body = json.dumps(REFUND_PROCESSED_PAYLOAD).encode()
		patcher = patch.object(RazorpaySettings, "get_password", return_value=self.secret)
		patcher.start()
		self.addCleanup(patcher.stop)

	def post(self, event_id: str, enqueue_fails: bool = False):
		request = MagicMock()
		request.data = self.body
		headers = {
			"X-Razorpay-Signature": sign(self.body, self.secret),
			"X-Razorpay-Event-Id": event_id,
		}

		with (
			patch.object(frappe, "request", request),
			patch.object(
				frappe,
				"get_request_header",
				side_effect=lambda key, default=None: headers.get(key, default),
			),
			patch.object(frappe.db, "commit"),
			patch.object(
				frappe,
				"enqueue",
				side_effect=Exception("redis is down") if enqueue_fails else None,
			),
		):
			return razorpay_webhook()

	def test_a_queue_that_is_down_reaches_razorpay_so_it_retries(self):
		# Answering 200 would drop the refund: Razorpay only resends what it did
		# not get a 2xx for.
		with self.assertRaises(Exception):
			self.post("evt_1", enqueue_fails=True)

	def test_a_redelivered_event_reuses_its_integration_request(self):
		with self.assertRaises(Exception):
			self.post("evt_1", enqueue_fails=True)

		self.post("evt_1")

		self.assertEqual(frappe.db.count("Integration Request", {"request_id": "evt_1"}), 1)
