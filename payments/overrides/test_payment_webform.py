# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.website.doctype.web_form.web_form import WebForm

from payments.overrides.payment_webform import PaymentWebForm, accept


class TestPaymentWebForm(IntegrationTestCase):
	def make_event_web_form(self):
		web_form = frappe.get_doc(
			{
				"doctype": "Web Form",
				"title": "_Test Payments Event Form",
				"route": "test-payments-event-form",
				"doc_type": "Event",
				"module": "Website",
				"published": 1,
				"login_required": 0,
				"web_form_fields": [
					{"fieldname": "subject", "fieldtype": "Data", "label": "Subject", "reqd": 1},
					{"fieldname": "description", "fieldtype": "Text", "label": "Description"},
					{"fieldname": "starts_on", "fieldtype": "Datetime", "label": "Starts On", "reqd": 1},
				],
			}
		).insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Web Form", web_form.name, force=True, ignore_permissions=True)
		return web_form

	def test_guest_cannot_choose_the_target_doctype(self):
		web_form = self.make_event_web_form()
		subject = "_Test Payments Web Form Target"
		frappe.set_user("Guest")
		self.addCleanup(frappe.set_user, "Administrator")

		with patch("frappe.utils.global_search.update_global_search"):
			doc = accept(
				web_form=web_form.name,
				data={
					"doctype": "ToDo",
					"subject": subject,
					"description": "Must be saved only as the configured Event",
					"starts_on": "2030-01-01 10:00:00",
				},
			)

		self.assertEqual(doc.doctype, "Event")
		self.assertEqual(doc.owner, "Guest")
		self.assertTrue(frappe.db.exists("Event", {"subject": subject}))
		self.assertFalse(
			frappe.db.exists("ToDo", {"description": "Must be saved only as the configured Event"})
		)

	@patch("payments.overrides.payment_webform.frappe_web_form_accept")
	def test_payment_redirect_wraps_frappe_accept(self, frappe_accept):
		response_doc = MagicMock()
		saved_doc = MagicMock()
		saved_doc.get.return_value = False
		frappe_accept.return_value = response_doc
		payment_web_form = MagicMock()
		payment_web_form.get.return_value = 1
		payment_web_form.get_payment_gateway_url.return_value = "https://pay.example/checkout"

		with (
			patch.object(frappe, "flags", frappe._dict(web_form_doc=saved_doc)),
			patch.object(frappe, "get_cached_doc", return_value=payment_web_form),
		):
			result = accept(
				web_form="_Test Payment Form",
				data={"doctype": "Event"},
				web_form_request_key="request-key",
				for_payment=False,
			)

		frappe_accept.assert_called_once_with(
			web_form="_Test Payment Form",
			data={"doctype": "Event"},
			web_form_request_key="request-key",
		)
		payment_web_form.get_payment_gateway_url.assert_called_once_with(saved_doc)
		self.assertEqual(result, "https://pay.example/checkout")

	@patch("payments.overrides.payment_webform.frappe_web_form_accept")
	def test_paid_document_skips_payment_redirect(self, frappe_accept):
		response_doc = MagicMock()
		saved_doc = MagicMock()
		saved_doc.get.return_value = True
		frappe_accept.return_value = response_doc
		payment_web_form = MagicMock()
		payment_web_form.get.return_value = 1

		with (
			patch.object(frappe, "flags", frappe._dict(web_form_doc=saved_doc)),
			patch.object(frappe, "get_cached_doc", return_value=payment_web_form),
		):
			result = accept(web_form="_Test Payment Form", data={}, for_payment=True)

		self.assertIs(result, response_doc)
		payment_web_form.get_payment_gateway_url.assert_not_called()

	@patch("payments.overrides.payment_webform.frappe_web_form_accept")
	def test_non_payment_web_form_ignores_client_payment_flag(self, frappe_accept):
		response_doc = MagicMock()
		saved_doc = MagicMock()
		saved_doc.get.return_value = False
		frappe_accept.return_value = response_doc
		payment_web_form = MagicMock()
		payment_web_form.get.return_value = 0

		with (
			patch.object(frappe, "flags", frappe._dict(web_form_doc=saved_doc)),
			patch.object(frappe, "get_cached_doc", return_value=payment_web_form),
		):
			result = accept(web_form="_Test Payment Form", data={}, for_payment=True)

		self.assertIs(result, response_doc)
		payment_web_form.get_payment_gateway_url.assert_not_called()

	def test_payment_validation_runs_during_submission_validation(self):
		web_form = PaymentWebForm({"doctype": "Web Form", "accept_payment": 1})
		doc = MagicMock()
		doc.get.return_value = False
		events = []

		with patch.object(WebForm, "validate_submission", autospec=True) as core_validation:
			core_validation.side_effect = lambda *args, **kwargs: events.append("core")
			doc.run_method.side_effect = lambda *args, **kwargs: events.append("payment")
			web_form.validate_submission(doc, uploaded_fields={"attachment"})

		core_validation.assert_called_once_with(web_form, doc, uploaded_fields={"attachment"})
		doc.run_method.assert_called_once_with("validate_payment")
		self.assertEqual(events, ["core", "payment"])
