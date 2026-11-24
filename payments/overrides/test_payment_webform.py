# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.overrides.payment_webform import accept


class TestPaymentWebFormAccept(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		self.web_form = frappe.get_doc(
			{
				"doctype": "Web Form",
				"title": "_Test Payments Web Form",
				"route": "_test-payments-web-form",
				"doc_type": "ToDo",
				"published": 1,
				"login_required": 0,
				"allow_multiple": 1,
				"web_form_fields": [
					{
						"fieldname": "description",
						"fieldtype": "Text",
						"label": "Description",
						"reqd": 1,
					}
				],
			}
		).insert(ignore_permissions=True)

		# `accept` is rate limited (ip based by default) and reads the
		# request/form_dict, which don't exist in unit tests.
		self._request = getattr(frappe.local, "request", None)
		self._request_ip = getattr(frappe.local, "request_ip", None)
		self._form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.request = frappe._dict(method="POST")
		frappe.local.request_ip = "127.0.0.1"
		frappe.local.form_dict = frappe._dict({"web_form": self.web_form.name})

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.local.request = self._request
		frappe.local.request_ip = self._request_ip
		frappe.local.form_dict = self._form_dict
		frappe.delete_doc("Web Form", self.web_form.name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": "_test payments guest todo"})

	def test_guest_submission_without_doctype_in_payload(self):
		# Standard Frappe web form payloads only carry field values; the DocType
		# has to come from the Web Form itself.
		frappe.set_user("Guest")
		doc = accept(
			web_form=self.web_form.name,
			data=json.dumps({"description": "_test payments guest todo"}),
		)
		frappe.set_user("Administrator")

		self.assertEqual(doc.doctype, "ToDo")
		self.assertTrue(frappe.db.exists("ToDo", {"description": "_test payments guest todo"}))
