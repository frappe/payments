# Copyright (c) 2018, Frappe Technologies and Contributors
# License: MIT. See LICENSE
#
# Regression tests for the Stripe checkout money-path guards. Each test pins a
# previously-exploitable hole shut so it cannot silently reopen:
#   - client-supplied amount (underpay)
#   - arbitrary / unauthorised reference
#   - PaymentIntent replay (settle order B with order A's payment)
#   - duplicate settlement (webhook vs browser-return race)
# They use Frappe core doctypes only, so they run without ERPNext installed.
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.templates.pages.stripe_checkout import get_reference_amount, guard_payment_reference


class TestStripeSettings(FrappeTestCase):
	def test_guard_rejects_missing_or_unknown_reference(self):
		"""guard_payment_reference blocks arbitrary/absent references (PermissionError)."""
		with self.assertRaises(frappe.PermissionError):
			guard_payment_reference(None, None)
		with self.assertRaises(frappe.PermissionError):
			guard_payment_reference("ToDo", "does-not-exist-000")

	def test_get_reference_amount_uses_server_side_grand_total(self):
		"""Amount is read from the reference's grand_total, never from client input."""
		with (
			patch.object(frappe, "get_meta") as mock_meta,
			patch.object(frappe.db, "get_value") as mock_get_value,
		):
			mock_meta.return_value.has_field.side_effect = lambda f: f in {"grand_total", "currency"}
			mock_get_value.return_value = frappe._dict({"grand_total": 100.0, "currency": "USD"})
			amount, currency = get_reference_amount("Payment Request", "PR-0001")
		self.assertEqual(amount, 100.0)
		self.assertEqual(currency, "USD")

	def test_get_reference_amount_rejects_amountless_doctype(self):
		"""A reference doctype with no payable amount field is rejected, not defaulted."""
		with self.assertRaises(frappe.ValidationError):
			get_reference_amount("ToDo", "any")

	def test_assert_intent_matches_reference_blocks_replay(self):
		"""A PaymentIntent minted for another order cannot settle this one."""
		settings = frappe.new_doc("Stripe Settings")
		settings.data = frappe._dict({"reference_doctype": "Payment Request", "reference_docname": "ORDER-B"})
		intent_for_other_order = {
			"metadata": {"reference_doctype": "Payment Request", "reference_docname": "ORDER-A"}
		}
		with self.assertRaises(frappe.PermissionError):
			settings.assert_intent_matches_reference(intent_for_other_order)

		# The intent minted for this very order is accepted (no raise).
		intent_for_this_order = {
			"metadata": {"reference_doctype": "Payment Request", "reference_docname": "ORDER-B"}
		}
		settings.assert_intent_matches_reference(intent_for_this_order)

	def test_claim_integration_request_settles_only_once(self):
		"""The webhook/redirect race can't double-settle: the claim is single-use."""
		ir = frappe.get_doc(
			{"doctype": "Integration Request", "integration_request_service": "Stripe"}
		).insert(ignore_permissions=True)

		settings = frappe.new_doc("Stripe Settings")
		settings.integration_request = ir

		# First caller wins the claim and flips the request to Completed.
		self.assertTrue(settings.claim_integration_request())
		self.assertEqual(frappe.db.get_value("Integration Request", ir.name, "status"), "Completed")

		# A concurrent second caller (already Completed) must lose the claim.
		self.assertFalse(settings.claim_integration_request())
