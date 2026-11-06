# Copyright (c) 2026, Frappe and Contributors
# See LICENSE
"""The Web Form override is the one production consumer of build_checkout_url.

It had no tests at all, which is how it went unnoticed that the kwargs it sends
omit `payment_gateway` — a key Stripe's checkout page requires.
"""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import frappe
from frappe.tests import IntegrationTestCase

from payments.payment_gateways.doctype.paytm_settings.paytm_settings import PaytmSettings
from payments.utils.utils import build_checkout_url


def _checkout_expected_keys() -> dict[str, tuple]:
	"""Every checkout page's expected_keys, discovered rather than listed.

	Discovery matters: a hardcoded list silently stops covering a page added
	later, which is the same instance-vs-class mistake this test exists to catch.
	The *_confirmation pages are excluded — they are return-URL handlers, not
	entry points the Web Form builds a URL for.
	"""
	import importlib
	import pathlib

	pages = pathlib.Path(frappe.get_app_path("payments")) / "templates" / "pages"
	found = {}
	for path in sorted(pages.glob("*_checkout.py")):
		module = importlib.import_module(f"payments.templates.pages.{path.stem}")
		keys = getattr(module, "expected_keys", None)
		if keys:
			found[path.stem] = keys
	return found


# Paytm is the v1 gateway used here because its settings doctype is a Single
# (so frappe.get_doc resolves it with no fixture and no credentials) and its
# get_payment_url only builds a URL. Nothing about the defect is Paytm-specific:
# it is the kwargs the Web Form sends, for every v1 gateway.
GATEWAY = "_Test Paytm Gateway"


class TestPaymentWebFormCheckout(IntegrationTestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.delete_doc("Payment Gateway", GATEWAY, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": GATEWAY,
				"gateway_settings": "Paytm Settings",
				"gateway_controller": "Paytm Settings",
			}
		).insert(ignore_permissions=True)
		cls.web_form = cls._make_web_form()
		cls.doc = frappe.get_doc({"doctype": "ToDo", "description": "web form payment probe"}).insert(
			ignore_permissions=True
		)

	@classmethod
	def _make_web_form(cls):
		frappe.delete_doc("Web Form", "test-payment-webform", force=True, ignore_permissions=True)
		return frappe.get_doc(
			{
				"doctype": "Web Form",
				"title": "test-payment-webform",
				"route": "test-payment-webform",
				"doc_type": "ToDo",
				"module": "Payments",
				"accept_payment": 1,
				"payment_gateway": GATEWAY,
				"amount": 42,
				"currency": "EUR",
				"web_form_fields": [
					{"fieldname": "description", "fieldtype": "Small Text", "label": "Description"}
				],
			}
		).insert(ignore_permissions=True)

	def test_the_override_is_mixed_into_web_form(self):
		"""Everything below depends on hooks.py's extend_doctype_class actually
		taking effect for a Web Form loaded from the database."""
		self.assertTrue(hasattr(self.web_form, "get_payment_gateway_url"))

	def test_the_gateway_name_reaches_the_v1_controller(self):
		"""Stripe's checkout page lists `payment_gateway` in expected_keys and
		redirect_to_message's "someone sent you to an incomplete URL" when any
		expected key is missing — so a Web Form payment through Stripe never
		reached a checkout. The Web Form's payment_details dict has never carried
		the key (it is absent on develop too, where the call was
		`controller.get_payment_url(**payment_details)`).
		"""
		url = self.web_form.get_payment_gateway_url(self.doc)
		query = parse_qs(urlparse(url).query)
		self.assertEqual(query.get("payment_gateway"), [GATEWAY])

	def test_the_webform_sends_every_key_a_v1_checkout_page_expects(self):
		"""The invariant behind the test above, stated where it can catch the next
		omission: whatever the Web Form sends must cover what a v1 checkout page
		requires.

		Asserted against the UNION of every checkout page's expected_keys, not
		Stripe's alone. Neither list is a superset of the other — Stripe requires
		payment_gateway and omits order_id; braintree, gocardless and razorpay do
		the reverse — so pinning one gateway would have missed a regression that
		dropped order_id.
		"""
		pages = _checkout_expected_keys()
		# Without this the test passes vacuously if discovery ever returns nothing.
		self.assertGreaterEqual(len(pages), 4, f"discovery found only {sorted(pages)}")
		required = set()
		for page, keys in pages.items():
			required |= set(keys)
			self.assertTrue(keys, f"{page} declared no expected_keys")
		self.assertIn("payment_gateway", required)
		self.assertIn("order_id", required)

		with patch.object(PaytmSettings, "get_payment_url", return_value="/probe") as sent:
			self.web_form.get_payment_gateway_url(self.doc)
		kwargs = sent.call_args.kwargs
		self.assertEqual(
			required - set(kwargs),
			set(),
			"the Web Form omitted a key a checkout page requires",
		)

	def test_the_builder_passes_the_gateway_name_on_to_a_v1_controller(self):
		"""The same invariant one layer down: build_checkout_url takes
		payment_gateway as a NAMED parameter, so it is no longer part of **kwargs
		and has to be forwarded explicitly."""
		with patch.object(PaytmSettings, "get_payment_url", return_value="/probe") as sent:
			build_checkout_url(payment_gateway=GATEWAY, amount=1, currency="EUR")
		self.assertEqual(sent.call_args.kwargs.get("payment_gateway"), GATEWAY)
