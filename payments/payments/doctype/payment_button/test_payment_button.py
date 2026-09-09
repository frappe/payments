# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import unittest

import frappe


class TestPaymentButtonProperty(unittest.TestCase):
	"""Verify requires_data_capture property exists and works.

	Regression: property was misspelled as requires_data_catpure,
	and was accessed on PSL instead of PaymentButton.
	"""

	def test_requires_data_capture_true_for_data_capture_variant(self):
		btn = frappe.new_doc("Payment Button")
		btn.implementation_variant = "Data Capture"
		self.assertTrue(btn.requires_data_capture)

	def test_requires_data_capture_false_for_widget_variant(self):
		btn = frappe.new_doc("Payment Button")
		btn.implementation_variant = "Third Party Widget"
		self.assertFalse(btn.requires_data_capture)

	def test_requires_data_capture_attribute_exists(self):
		"""Property should be accessible — not raise AttributeError."""
		btn = frappe.new_doc("Payment Button")
		# This would have raised AttributeError with the old typo
		_ = btn.requires_data_capture
