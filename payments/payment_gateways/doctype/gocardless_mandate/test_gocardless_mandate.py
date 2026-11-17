# Copyright (c) 2018, Frappe Technologies and Contributors
# See license.txt

import frappe
from frappe.tests.utils import FrappeTestCase

from payments.templates.pages.gocardless_confirmation import create_mandate


def _make_payment_request(customer_name):
	"""A Customer -> Sales Invoice -> Payment Request chain, as the GoCardless
	confirmation flow expects, so create_mandate() can resolve the reference."""
	customer = frappe.get_doc({"doctype": "Customer", "customer_name": customer_name}).insert(
		ignore_permissions=True
	)
	si = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": "_Test Company",
			"customer": customer.name,
			"currency": "INR",
			"items": [{"item_code": "Consulting", "qty": 1, "rate": 100}],
		}
	)
	si.flags.ignore_mandatory = True
	si.insert(ignore_permissions=True)
	pr = frappe.get_doc(
		{
			"doctype": "Payment Request",
			"payment_request_type": "Inward",
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"party_type": "Customer",
			"party": customer.name,
			"grand_total": 100,
			"currency": "INR",
			"email_to": "x@example.com",
		}
	)
	pr.flags.ignore_mandatory = True
	pr.flags.ignore_validate = True
	pr.insert(ignore_permissions=True)
	return customer, pr


class TestGoCardlessMandate(FrappeTestCase):
	def test_failed_mandate_creation_logs_transaction_context(self):
		"""When create_mandate() can't persist a mandate, the Error Log entry must
		spell out the transaction (reference doc, customer, mandate id) in a readable
		summary so the failure is actionable on its own -- the bare traceback Frappe
		captures by default buries those values in a locals dump (#89 diagnosis)."""
		customer, pr = _make_payment_request("_Test GC Diag Customer")

		# Force a failure inside create_mandate's insert: gocardless_customer is reqd,
		# so an empty value raises MandatoryError, which create_mandate swallows.
		create_mandate(
			{
				"mandate": "MD-DIAG-1",
				"customer": "",
				"reference_doctype": "Payment Request",
				"reference_docname": pr.name,
			}
		)
		self.assertFalse(frappe.db.exists("GoCardless Mandate", "MD-DIAG-1"))

		log = frappe.get_all(
			"Error Log",
			filters={"method": ("like", "%nable to create mandate%")},
			fields=["error"],
			order_by="creation desc",
			limit=1,
		)
		self.assertTrue(log, "expected an Error Log entry for the failed mandate creation")
		error = log[0]["error"]
		# The readable summary line the enrichment adds (distinct from a locals dump).
		self.assertIn(f"Could not save GoCardless Mandate MD-DIAG-1 for Payment Request {pr.name}", error)
		self.assertIn(f"customer: {customer.customer_name}", error)
