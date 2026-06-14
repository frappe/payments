# Copyright (c) 2018, Frappe Technologies and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase


class TestGoCardlessSettings(FrappeTestCase):
	def _use_customer_naming_series(self):
		original = frappe.db.get_default("cust_master_name")
		frappe.db.set_default("cust_master_name", "Naming Series")
		frappe.clear_cache()
		self.addCleanup(
			lambda: (frappe.db.set_default("cust_master_name", original or ""), frappe.clear_cache())
		)

	def test_get_registered_mandate_keys_on_customer_docname(self):
		"""A stored mandate must be retrievable by the ERPNext Customer *docname*.
		Keying on customer_name (display) misses the record under a Customer naming
		series, so reuse falls through and the payer is re-prompted (#89)."""
		self._use_customer_naming_series()
		customer = frappe.get_doc(
			{"doctype": "Customer", "customer_name": "Reg Mandate Co", "naming_series": "CUST-.YYYY.-"}
		).insert(ignore_permissions=True)
		self.assertNotEqual(customer.name, customer.customer_name)  # precondition
		frappe.get_doc(
			{
				"doctype": "GoCardless Mandate",
				"mandate": "MD-REG-1",
				"customer": customer.name,
				"gocardless_customer": "CU-REG-1",
			}
		).insert(ignore_permissions=True)

		settings = frappe.new_doc("GoCardless Settings")
		# Found by docname...
		self.assertEqual(settings.get_registered_mandate(customer.name), "MD-REG-1")
		# ...not by the display name, and tolerant of a missing/empty customer.
		self.assertIsNone(settings.get_registered_mandate(customer.customer_name))
		self.assertIsNone(settings.get_registered_mandate(None))

	def test_checkout_resolves_payer_from_display_name(self):
		"""The checkout URL carries the Customer display name (Payment Request sends
		customer_name as payer_name), so resolving the payer must tolerate
		docname != customer_name under a Customer naming series, otherwise the very
		first subscription dies before a mandate can be created (#89)."""
		from payments.templates.pages.gocardless_checkout import get_payer_customer

		self._use_customer_naming_series()
		customer = frappe.get_doc(
			{"doctype": "Customer", "customer_name": "Checkout Payer Co", "naming_series": "CUST-.YYYY.-"}
		).insert(ignore_permissions=True)
		self.assertNotEqual(customer.name, customer.customer_name)  # precondition

		resolved = get_payer_customer(customer.customer_name)
		self.assertEqual(resolved.name, customer.name)
