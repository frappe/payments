# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE
from unittest.mock import MagicMock, patch

import frappe
import razorpay
from frappe.tests import IntegrationTestCase, UnitTestCase

from payments.payment_gateways.doctype.razorpay_settings.razorpay_settings import (
	RazorpaySettings,
	from_paise,
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

	def test_fetch_refunds_returns_the_refunds_on_a_payment(self):
		client = MagicMock()
		client.payment.fetch_multiple_refund.return_value = {
			"entity": "collection",
			"count": 1,
			"items": [{"id": "rfnd_123", "status": "processed", "amount": 5000}],
		}

		with patch.object(RazorpaySettings, "get_client", return_value=client):
			refunds = self.settings.fetch_refunds("pay_123")

		client.payment.fetch_multiple_refund.assert_called_once_with("pay_123")
		self.assertEqual([refund["id"] for refund in refunds], ["rfnd_123"])

	def test_fetch_refunds_is_empty_when_the_payment_has_none(self):
		client = MagicMock()
		client.payment.fetch_multiple_refund.return_value = {"entity": "collection", "count": 0, "items": []}

		with patch.object(RazorpaySettings, "get_client", return_value=client):
			self.assertEqual(self.settings.fetch_refunds("pay_123"), [])


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
