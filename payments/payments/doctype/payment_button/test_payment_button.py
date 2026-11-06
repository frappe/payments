# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import unittest
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.payments.doctype.payment_button.payment_button import PaymentButton


class TestPaymentButtonFrontendSafeContext(unittest.TestCase):
	"""H1: gateway secrets must never reach guest-rendered templates.

	The raw gateway settings document holds API secrets (secret_key etc.).
	_frontend_safe_doc() must expose ONLY the controller's whitelisted,
	non-secret projection — never the raw doc.
	"""

	def test_secret_key_not_in_projection(self):
		btn = PaymentButton.__new__(PaymentButton)
		btn.gateway_settings = "Fake Settings"
		btn.gateway_controller = "Fake Controller"

		# A gateway-settings-like object: holds a secret + a publishable key,
		# and exposes only the publishable key as frontend-safe.
		class FakeController:
			secret_key = "sk_live_SUPERSECRET"
			publishable_key = "pk_live_safe"

			def get_frontend_safe_context(self):
				return {"publishable_key": self.publishable_key}

		with patch("frappe.get_cached_doc", return_value=FakeController()):
			safe = btn._frontend_safe_doc()

		self.assertEqual(safe.get("publishable_key"), "pk_live_safe")
		self.assertNotIn("secret_key", safe)
		self.assertNotIn("sk_live_SUPERSECRET", str(safe))

	def test_default_projection_is_empty(self):
		btn = PaymentButton.__new__(PaymentButton)
		btn.gateway_settings = "Fake Settings"
		btn.gateway_controller = "Fake Controller"

		# Default controller projection exposes nothing.
		class BareController:
			secret_key = "sk_live_SUPERSECRET"

			def get_frontend_safe_context(self):
				return {}

		with patch("frappe.get_cached_doc", return_value=BareController()):
			safe = btn._frontend_safe_doc()

		self.assertEqual(dict(safe), {})


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


class TestPaymentButtonAssetRendering(unittest.TestCase):
	"""The gateway_* fields are System-Manager-authored Jinja, rendered into the
	public /pay page. These are the frappe-ssti-suppressed render sites, and the
	suppression is only sound while the render context stays the frontend-safe
	projection — so assert the projection reaches the template and the raw
	settings doc does not.
	"""

	class _Controller:
		secret_key = "sk_live_SUPERSECRET"
		publishable_key = "pk_live_safe"

		def get_frontend_safe_context(self):
			return {"publishable_key": self.publishable_key}

	def _button(self):
		# gateway_js / data_capture deliberately reference doc.secret_key: a
		# gateway template could, and the assertion below is only meaningful if
		# the template actually asks for it.
		btn = PaymentButton.__new__(PaymentButton)
		btn.gateway_settings = "Fake Settings"
		btn.gateway_controller = "Fake Controller"
		btn.gateway_css = ".key:after{content:'{{ doc.publishable_key }}'}"
		btn.gateway_js = (
			"var key='{{ doc.publishable_key }}'; var leak='{{ doc.secret_key }}';"
			" var amount={{ payload.amount }};"
		)
		btn.gateway_wrapper = "<div data-key='{{ doc.publishable_key }}'></div>"
		btn.data_capture = (
			"<form data-key='{{ doc.publishable_key }}' data-status='{{ status }}'"
			" data-leak='{{ doc.secret_key }}'>{{ extra.foo }}</form>"
		)
		btn.extra_payload = '{"foo": "captured"}'
		return btn

	def test_widget_assets_render_projection_and_payload(self):
		payload = frappe._dict({"amount": 25})
		with patch("frappe.get_cached_doc", return_value=self._Controller()):
			css, js, wrapper = self._button().get_widget_assets(payload)

		for rendered in (css, js, wrapper):
			self.assertIn("pk_live_safe", rendered)
			self.assertNotIn("sk_live_SUPERSECRET", rendered)
		self.assertIn("amount=25", js)

	def test_data_capture_assets_render_projection_state_and_extra(self):
		with patch("frappe.get_cached_doc", return_value=self._Controller()):
			rendered = self._button().get_data_capture_assets({"status": "Started"})

		self.assertIn("pk_live_safe", rendered)
		self.assertIn("Started", rendered)
		self.assertIn("captured", rendered)
		self.assertNotIn("sk_live_SUPERSECRET", rendered)


class TestPaymentButtonGuestPermissions(IntegrationTestCase):
	"""Guest gets NO permission on Payment Button.

	The earlier version of this granted Guest `read` + `select` and justified it
	by /pay using frappe.get_list, and blamed the bulk-read exposure on `export`
	and `report`. That was wrong on both counts: with `read` alone, an anonymous
	`frappe.get_list("Payment Button", fields=[...])` returns `gateway_js`,
	`extra_payload` and `data_capture` in full over the REST API, so plain `read`
	IS the bulk read. And the payer on /pay is authorised by holding the session
	capability URL, not by a role — so pay.py uses frappe.get_all and needs no
	Guest permission at all.
	"""

	BUTTON = "_Test Guest Perm Button"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.delete_doc("Payment Button", cls.BUTTON, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Payment Button",
				"label": cls.BUTTON,
				"enabled": 1,
				"gateway_settings": "Payment Demo Settings",
				"gateway_controller": "Payment Demo Settings",
				"implementation_variant": "Third Party Widget",
				"gateway_js": "SECRET_JS_MARKER",
				"extra_payload": '{"merchant": "SECRET_EXTRA_MARKER"}',
			}
		).insert(ignore_permissions=True)

	def test_the_doctype_grants_guest_nothing(self):
		import json
		import pathlib

		meta = json.loads(
			(
				pathlib.Path(frappe.get_app_path("payments"))
				/ "payments"
				/ "doctype"
				/ "payment_button"
				/ "payment_button.json"
			).read_text()
		)
		guest = [p for p in meta["permissions"] if p.get("role") == "Guest"]
		self.assertEqual(guest, [], f"Guest was granted {guest}")

	def test_a_guest_cannot_read_gateway_templates_over_the_orm(self):
		"""The reachability assertion the registry check above cannot make."""
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")
		with self.assertRaises(frappe.PermissionError):
			frappe.get_list(
				"Payment Button",
				fields=["name", "gateway_js", "extra_payload", "data_capture"],
			)
