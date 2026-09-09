# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.payments.doctype.payment_session_log.payment_session_log import (
	PaymentSessionLog,
	select_button,
)


class TestPaymentSessionLogTerminalStates(unittest.TestCase):
	"""Unit tests for terminal state methods."""

	def test_is_terminal_returns_true_for_paid(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Paid"
		self.assertTrue(psl.is_terminal())

	def test_is_terminal_returns_true_for_declined(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Declined"
		self.assertTrue(psl.is_terminal())

	def test_is_terminal_returns_false_for_created(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Created"
		self.assertFalse(psl.is_terminal())

	def test_is_terminal_returns_false_for_initiated(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Initiated"
		self.assertFalse(psl.is_terminal())

	def test_get_indicator_color_returns_green_for_paid(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Paid"
		self.assertEqual(psl.get_indicator_color(), "green")

	def test_get_indicator_color_returns_red_for_error(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Error"
		self.assertEqual(psl.get_indicator_color(), "red")

	def test_get_indicator_color_returns_gray_for_unknown(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "SomeUnknownStatus"
		self.assertEqual(psl.get_indicator_color(), "gray")


class TestSelectButtonAuthorization(IntegrationTestCase):
	"""Integration tests for select_button security validations."""

	def setUp(self):
		# Create a test Stripe Settings (Gateway Controller) first
		if not frappe.db.exists("Stripe Settings", "_Test Controller"):
			self.controller = frappe.get_doc(
				{
					"doctype": "Stripe Settings",
					"gateway_name": "_Test Controller",
					"publishable_key": "pk_test_dummy",
					"secret_key": "sk_test_dummy",
				}
			)
			self.controller.flags.ignore_mandatory = True  # Skip API key validation
			self.controller.insert(ignore_permissions=True)

		# Create a test payment button (autoname from label)
		if not frappe.db.exists("Payment Button", "_Test PSL Button"):
			self.btn = frappe.get_doc(
				{
					"doctype": "Payment Button",
					"label": "_Test PSL Button",
					"enabled": 1,
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "_Test Controller",
				}
			)
			self.btn.insert(ignore_permissions=True)
		else:
			self.btn = frappe.get_doc("Payment Button", "_Test PSL Button")

		# Create a disabled button for testing
		if not frappe.db.exists("Payment Button", "_Test PSL Disabled"):
			self.disabled_btn = frappe.get_doc(
				{
					"doctype": "Payment Button",
					"label": "_Test PSL Disabled",
					"enabled": 0,
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "_Test Controller",
				}
			)
			self.disabled_btn.insert(ignore_permissions=True)

	def _create_psl(self, status="Created", gateway=None):
		"""Helper to create a test PSL."""
		psl = frappe.get_doc(
			{
				"doctype": "Payment Session Log",
				"status": status,
				"tx_data": json.dumps({"amount": 100, "currency": "USD"}),
				"gateway": gateway,
			}
		)
		psl.insert(ignore_permissions=True)
		return psl

	def test_select_button_rejects_disabled_button(self):
		"""select_button should reject disabled buttons."""
		psl = self._create_psl()

		result = select_button(pslName=psl.name, buttonName="_Test PSL Disabled")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_rejects_terminal_psl(self):
		"""select_button should reject PSL in terminal state."""
		psl = self._create_psl(status="Paid")

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_rejects_mismatched_gateway(self):
		"""select_button should reject button that doesn't match PSL gateway filter."""
		# PSL requires a specific gateway
		gateway_filter = json.dumps(
			{
				"gateway_settings": "GoCardless Settings",
				"gateway_controller": "Different Controller",
			}
		)
		psl = self._create_psl(gateway=gateway_filter)

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_accepts_valid_selection(self):
		"""select_button should accept valid button selection."""
		psl = self._create_psl()

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNotNone(result)
		self.assertTrue(result.get("reload"))
		psl.reload()
		self.assertEqual(psl.button, "_Test PSL Button")

	def test_select_button_accepts_matching_gateway(self):
		"""select_button should accept button matching PSL gateway filter."""
		gateway_filter = json.dumps(
			{
				"gateway_settings": "Stripe Settings",
				"gateway_controller": "_Test Controller",
			}
		)
		psl = self._create_psl(gateway=gateway_filter)

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNotNone(result)
		psl.reload()
		self.assertEqual(psl.button, "_Test PSL Button")
