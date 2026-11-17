# Copyright (c) 2026, Frappe and Contributors
# See LICENSE
"""The shared v1/v2 boundary in payments/utils/utils.py.

This module had no tests at all, which is why a v2 gateway reaching
get_checkout_url failed invisibly: PaymentController.get_payment_url is a
staticmethod taking a session name, the v1 contract is an instance method taking
**kwargs, and the mismatch raised TypeError into a bare `except` that logged
nothing.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.utils.utils import build_checkout_url, get_checkout_url, is_v2_gateway


class TestGatewayGenerationBoundary(IntegrationTestCase):
	TITLE = "Could not build a checkout URL"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.v2_gateway = "Demo-Gateway-Utils"
		if not frappe.db.exists("Payment Gateway", cls.v2_gateway):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway",
					"gateway": cls.v2_gateway,
					"gateway_settings": "Payment Demo Settings",
					"gateway_controller": "Payment Demo Settings",
				}
			).insert(ignore_permissions=True)

		# Two v1 gateways that differ ONLY in how they have to be resolved. Paytm
		# because its settings doctype is a Single and its get_payment_url just
		# builds a URL, so neither needs credentials or a fixture row.
		cls.v1_single_instance = "Paytm"  # resolves as f"{gateway} Settings"
		cls.v1_multi_instance = "Paytm-_Test Instance"  # only via gateway_controller
		for gateway, fields in (
			(cls.v1_single_instance, {}),
			(
				cls.v1_multi_instance,
				{"gateway_settings": "Paytm Settings", "gateway_controller": "Paytm Settings"},
			),
		):
			frappe.delete_doc("Payment Gateway", gateway, force=True, ignore_permissions=True)
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": gateway, **fields}).insert(
				ignore_permissions=True
			)

	# -- classification --

	def test_a_v2_gateway_is_classified_as_v2(self):
		self.assertTrue(is_v2_gateway(self.v2_gateway))

	def test_a_non_controller_settings_doctype_is_classified_as_v1(self):
		"""The true negative for the isinstance branch: a gateway whose settings
		doctype does not inherit PaymentController.

		Uses an ordinary doctype rather than a real v1 gateway on purpose — every
		one of those validates credentials on insert, which would make this test
		depend on this bench's gateway keys and behave differently in CI. What
		matters here is the isinstance check, and a plain Document exercises it.
		A nonexistent name and None do not: they hit the except guard.
		"""
		v1_gateway = "_Test Not A Controller"
		frappe.delete_doc("Payment Gateway", v1_gateway, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": v1_gateway,
				"gateway_settings": "User",
				"gateway_controller": "Administrator",
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Payment Gateway", v1_gateway, force=True, ignore_permissions=True)

		self.assertFalse(is_v2_gateway(v1_gateway), "a non-PaymentController gateway was classified as v2")

	def test_junk_input_is_tolerated(self):
		self.assertFalse(is_v2_gateway("does-not-exist"))
		self.assertFalse(is_v2_gateway(None))

	# -- the server-side builder --

	def test_the_builder_produces_a_url_for_a_v2_gateway(self):
		"""The v1 call shape used to raise TypeError into a silent except and
		answer a generic "something is wrong with this site's configuration"
		page."""
		url = build_checkout_url(
			payment_gateway=self.v2_gateway,
			amount=25.00,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
		)
		self.assertTrue(url, "no checkout URL was produced for a v2 gateway")
		self.assertIn("/pay?", url)

	def test_the_builder_records_what_it_was_given(self):
		"""It creates the money spine from its arguments, so pin what lands
		there — this is the assertion that makes the trust boundary visible."""
		import json

		before = set(frappe.get_all("Payment Session Log", pluck="name"))
		build_checkout_url(
			payment_gateway=self.v2_gateway,
			amount=33.00,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
			payer_name="Ada",
			payer_email="ada@example.com",
		)
		created = set(frappe.get_all("Payment Session Log", pluck="name")) - before
		self.assertEqual(len(created), 1)
		tx = json.loads(frappe.db.get_value("Payment Session Log", created.pop(), "tx_data"))
		self.assertEqual(tx["amount"], 33.00)
		self.assertEqual(tx["currency"], "EUR")
		self.assertEqual(tx["reference_doctype"], "User")
		self.assertEqual(tx["reference_docname"], "Administrator")
		self.assertEqual(tx["payer_contact"]["full_name"], "Ada")
		# payer_email becomes email_id, and `email` is deliberately NOT allowlisted.
		self.assertEqual(tx["payer_contact"]["email_id"], "ada@example.com")
		self.assertNotIn("email", tx["payer_contact"])

	def test_the_builder_is_not_itself_a_whitelisted_endpoint(self):
		"""Because it creates a Payment Session Log from its arguments, it must not
		be dispatchable from a request.

		Scope: this asserts the REGISTRY, which is all whitelisting governs —
		frappe consults it only at request entry. It is not a reachability proof: a
		guest still reaches this function through payment_webform.accept, where the
		amount, currency and reference come from the Web Form's configuration
		rather than from the caller. See test_payment_webform.py.
		"""
		self.assertNotIn(build_checkout_url, frappe.whitelisted)
		self.assertNotIn(build_checkout_url, frappe.guest_methods)

	# -- the guest endpoint --

	def test_a_guest_cannot_create_a_session_through_the_endpoint(self):
		"""get_checkout_url is guest-callable and used to take amount, currency and
		the reference document straight from caller-supplied kwargs and create a
		real Payment Session Log from them. So an unauthenticated GET could open a
		session for an arbitrary amount against an arbitrary document — and those
		rows sit at "Created", which is not in DISPOSABLE_STATES, so retention
		never removes them.
		"""
		before = frappe.db.count("Payment Session Log")
		frappe.set_user("Guest")
		try:
			get_checkout_url(
				payment_gateway=self.v2_gateway,
				amount=0.01,
				currency="EUR",
				reference_doctype="Payment Request",
				reference_docname="ATTACKER-CHOSEN",
			)
		except Exception:
			pass
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(
			frappe.db.count("Payment Session Log"),
			before,
			"a guest created a payment session with attacker-chosen amount and reference",
		)

	def test_the_guest_endpoint_does_not_resolve_a_multi_instance_gateway(self):
		"""Held at develop's exposure. develop resolved this endpoint's gateway as
		frappe.get_doc(f"{gateway} Settings"), which cannot reach a gateway whose
		settings live under a different doctype name — every multi-instance v1
		gateway: Stripe-<name>, Braintree, GoCardless, Mpesa. Resolving them
		through get_payment_gateway_controller instead would let an unauthenticated
		caller mint a checkout URL for an arbitrary amount against an arbitrary
		reference document on gateways develop could not reach from here.
		"""
		# Two things this test must NOT do, both found by mutation because each made
		# it pass with the wide resolution restored:
		#   - run as Guest: which gateways resolve is user-independent, and a Guest
		#     cannot insert the Integration Request either way, so both paths
		#     return None;
		#   - name a reference document that does not exist: the v1 path builds an
		#     Integration Request with a Dynamic Link, so a bogus docname raises
		#     LinkValidationError into the same `except` and returns None too.
		# The resolution has to be the only thing that can differ.
		before = frappe.db.count("Integration Request")
		url = get_checkout_url(
			payment_gateway=self.v1_multi_instance,
			amount=0.01,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
		)
		self.assertIsNone(url, "the endpoint resolved a multi-instance gateway")
		self.assertEqual(frappe.db.count("Integration Request"), before)

	def test_the_guest_endpoint_still_serves_a_single_instance_gateway(self):
		"""Control: the narrowing above must not take away what develop served."""
		url = get_checkout_url(
			payment_gateway=self.v1_single_instance,
			amount=1,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
		)
		self.assertIn("paytm_checkout", url or "")

	def test_the_builder_still_resolves_a_multi_instance_gateway(self):
		"""The asymmetry is the point of the split: the server-side builder keeps
		the general resolution, because its callers are not guests."""
		url = build_checkout_url(
			payment_gateway=self.v1_multi_instance,
			amount=1,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
		)
		self.assertIn("paytm_checkout", url or "")

	# -- the Payment Request custom field, and its patch --

	def test_the_custom_field_helper_is_idempotent(self):
		"""The patch calls it on every migrate, so a second run must be a no-op
		rather than a duplicate field or a throw."""
		from payments.utils.utils import make_payment_request_custom_fields

		make_payment_request_custom_fields()
		before = frappe.db.count("Custom Field", {"name": "Payment Request-payment_session_log"})
		make_payment_request_custom_fields()
		self.assertEqual(
			frappe.db.count("Custom Field", {"name": "Payment Request-payment_session_log"}), before
		)
		self.assertEqual(before, 1, "the field was not created at all")

	def test_the_helper_is_a_no_op_without_erpnext(self):
		"""This app does not require ERPNext, and frappe.get_meta RAISES for an
		absent doctype — so without the guard the patch aborts every bench migrate
		on such a site.

		Both halves have to be simulated. An earlier version patched only
		frappe.db.exists, and mutation showed it survived removing the guard:
		get_meta("Payment Request") still succeeded on a bench that has ERPNext,
		so the test never reached the call the guard exists to prevent.
		"""
		from payments.utils.utils import make_payment_request_custom_fields

		real_exists, real_get_meta = frappe.db.exists, frappe.get_meta

		def _absent_exists(*args, **kwargs):
			if args[:2] == ("DocType", "Payment Request"):
				return None
			return real_exists(*args, **kwargs)

		def _absent_get_meta(doctype, *args, **kwargs):
			if doctype == "Payment Request":
				raise frappe.DoesNotExistError(f"DocType {doctype} not found")
			return real_get_meta(doctype, *args, **kwargs)

		with patch.object(frappe.db, "exists", _absent_exists):
			with patch.object(frappe, "get_meta", _absent_get_meta):
				make_payment_request_custom_fields()  # must not raise

	def test_a_configuration_failure_is_not_swallowed_silently(self):
		"""Control on the diagnostic, counting only our own rows: a global Error
		Log count is satisfied by any unrelated log and never checks what was
		recorded."""
		before = frappe.db.count("Error Log", {"method": self.TITLE})
		get_checkout_url(payment_gateway="no-such-gateway", amount=1, currency="EUR")
		self.assertGreater(
			frappe.db.count("Error Log", {"method": self.TITLE}),
			before,
			"a configuration failure left no trace",
		)
		latest = frappe.get_all(
			"Error Log",
			filters={"method": self.TITLE},
			fields=["error"],
			order_by="creation desc",
			limit=1,
		)
		self.assertIn("Traceback", latest[0].error, "no traceback was recorded")
