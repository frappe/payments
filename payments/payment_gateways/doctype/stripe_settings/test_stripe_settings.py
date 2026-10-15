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
from unittest.mock import MagicMock, patch

import frappe
import stripe
from frappe.tests.utils import FrappeTestCase

from payments.payment_gateways.stripe_utils import (
	get_or_create_customer,
	get_stripe_settings_for_gateway,
)
from payments.templates.pages.stripe_checkout import get_reference_amount, guard_payment_reference

STRIPE_SETTINGS = "payments.payment_gateways.doctype.stripe_settings.stripe_settings"

# Real Stripe PaymentIntent service class. Specing service mocks against it makes a
# call to a method that does not exist (e.g. the old resource-API .modify) raise
# AttributeError instead of MagicMock silently fabricating it.
_PAYMENT_INTENT_SERVICE = type(stripe.StripeClient("sk_test_dummy").payment_intents)


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

	def test_enable_setup_future_usage_requires_matching_client_secret(self):
		"""Consent can only be set with the intent's own client_secret (ownership proof)."""
		settings = frappe.new_doc("Stripe Settings")
		intent = frappe._dict(
			{
				"client_secret": "pi_1_secret_ok",
				"status": "requires_confirmation",
				"customer": "cus_1",
				"metadata": {"reference_doctype": "Payment Request", "reference_docname": "ORDER-A"},
			}
		)
		client = MagicMock()
		client.payment_intents = MagicMock(spec=_PAYMENT_INTENT_SERVICE)
		client.payment_intents.retrieve.return_value = intent
		with patch(STRIPE_SETTINGS + ".get_stripe_client", return_value=client):
			with self.assertRaises(frappe.PermissionError):
				settings.enable_setup_future_usage("pi_1", "wrong", "Payment Request", "ORDER-A")
			client.payment_intents.update.assert_not_called()
			result = settings.enable_setup_future_usage(
				"pi_1", "pi_1_secret_ok", "Payment Request", "ORDER-A"
			)
		self.assertTrue(result["updated"])
		client.payment_intents.update.assert_called_once()

	def test_gateway_resolution_walks_account_chain(self):
		"""Payment Gateway Account -> Payment Gateway -> Stripe Settings resolves the controller."""
		with (
			patch.object(frappe.db, "get_value") as gv,
			patch.object(frappe, "get_doc", return_value=frappe._dict({"name": "Stripe"})) as gd,
		):
			gv.side_effect = [
				"Stripe-Stripe",
				frappe._dict({"gateway_settings": "Stripe Settings", "gateway_controller": "Stripe"}),
			]
			doc = get_stripe_settings_for_gateway("Stripe-Stripe - INR - CW")
		self.assertEqual(doc.name, "Stripe")
		gd.assert_called_once_with("Stripe Settings", "Stripe")

	def test_gateway_resolution_falls_back_to_sole_record(self):
		"""A blank gateway_controller resolves to the only Stripe Settings record."""
		with (
			patch.object(frappe.db, "get_value") as gv,
			patch.object(frappe, "get_all", return_value=["Stripe"]),
			patch.object(frappe, "get_doc", return_value=frappe._dict({"name": "Stripe"})),
		):
			gv.side_effect = [
				"Stripe-Stripe",
				frappe._dict({"gateway_settings": "Stripe Settings", "gateway_controller": None}),
			]
			doc = get_stripe_settings_for_gateway("acct")
		self.assertEqual(doc.name, "Stripe")

	def test_gateway_resolution_ignores_non_stripe(self):
		"""A non-Stripe gateway returns None so the sync is skipped, not misrouted."""
		with patch.object(frappe.db, "get_value") as gv:
			gv.side_effect = [
				"Razorpay-X",
				frappe._dict({"gateway_settings": "Razorpay Settings", "gateway_controller": "X"}),
			]
			self.assertIsNone(get_stripe_settings_for_gateway("acct"))

	def test_get_or_create_customer_does_not_duplicate_on_transient_error(self):
		"""A transient Stripe error on lookup propagates instead of creating a duplicate."""
		client = MagicMock()
		client.customers.retrieve.side_effect = stripe.error.AuthenticationError("bad key")
		with (
			patch.object(frappe.db, "has_column", return_value=True),
			patch.object(frappe.db, "get_value", return_value="cus_old"),
		):
			with self.assertRaises(stripe.error.AuthenticationError):
				get_or_create_customer(client, customer="ACME")
		client.customers.create.assert_not_called()

	def test_get_or_create_customer_recovers_from_stale_id(self):
		"""A stale/deleted id (InvalidRequestError) falls through to recreate."""
		client = MagicMock()
		client.customers.retrieve.side_effect = stripe.error.InvalidRequestError("no such customer", "id")
		client.customers.search.return_value = frappe._dict({"data": []})
		client.customers.create.return_value = frappe._dict({"id": "cus_new"})
		with (
			patch.object(frappe.db, "has_column", return_value=True),
			patch.object(frappe.db, "get_value", return_value="cus_stale"),
			patch.object(frappe.db, "set_value"),
		):
			cid = get_or_create_customer(client, customer="ACME")
		self.assertEqual(cid, "cus_new")
		client.customers.create.assert_called_once()
