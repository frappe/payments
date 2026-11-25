from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.utils import invoice_links

GATEWAY = "_Test Invoice Link Gateway"
PAYMENT_URL = "https://pay.example.com/checkout"


def set_payment_url(self, *args, **kwargs):
	self.payment_url = f"{PAYMENT_URL}?request={self.name}"


class TestInvoiceLinks(IntegrationTestCase):
	def setUp(self):
		if not invoice_links.erpnext_installed():
			self.skipTest("ERPNext isn't installed")
		from erpnext.accounts.doctype.sales_invoice.test_sales_invoice import create_sales_invoice

		invoice_links.make_fields()
		frappe.clear_cache(doctype="Accounts Settings")
		self.set_mode(invoice_links.ALL, emails=1, prints=1)
		if not frappe.db.exists("Payment Gateway", GATEWAY):
			frappe.get_doc({"doctype": "Payment Gateway", "gateway": GATEWAY}).insert(ignore_permissions=True)
		if not frappe.db.exists("Payment Gateway Account", {"payment_gateway": GATEWAY}):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway Account",
					"payment_gateway": GATEWAY,
					"payment_account": "_Test Bank - _TC",
					"company": "_Test Company",
					"currency": "INR",
					"payment_channel": "Email",
				}
			).insert(ignore_permissions=True)
		self.invoice = create_sales_invoice(customer="_Test Customer", rate=500, qty=2)
		for doctype, name in (("Customer", "_Test Customer"), ("Customer Group", "_Test Customer Group")):
			frappe.db.set_value(doctype, name, "invoice_payment_links", None)

	def set_mode(self, mode, emails=1, prints=1):
		frappe.db.set_single_value(
			"Accounts Settings",
			{
				"invoice_payment_links": mode,
				"payment_link_in_emails": emails,
				"payment_link_in_prints": prints,
			},
		)

	def test_settings_never_saved_have_links_on_in_emails_and_prints(self):
		frappe.db.delete(
			"Singles",
			{
				"doctype": "Accounts Settings",
				"field": [
					"in",
					["invoice_payment_links", "payment_link_in_emails", "payment_link_in_prints"],
				],
			},
		)
		self.assertEqual(
			invoice_links.settings(),
			{
				"invoice_payment_links": invoice_links.ALL,
				"payment_link_in_emails": 1,
				"payment_link_in_prints": 1,
			},
		)
		self.set_mode(invoice_links.ALL, emails=0, prints=1)
		self.assertEqual(invoice_links.settings().payment_link_in_emails, 0)

	def test_an_unpaid_invoice_has_a_private_link(self):
		link = self.invoice.payment_link
		self.assertTrue(link.startswith(frappe.utils.get_url("/pay_invoice?")))
		self.assertIn(f"invoice={self.invoice.name}", link)
		key = link.rsplit("key=", 1)[1]
		self.assertTrue(invoice_links.valid_key("Sales Invoice", self.invoice.name, key))
		self.assertFalse(invoice_links.valid_key("Sales Invoice", self.invoice.name, key[:-1] + "0"))
		self.assertFalse(invoice_links.valid_key("Sales Invoice", "ACC-SINV-OTHER", key))

	def test_who_gets_links_follows_the_settings_then_the_group_then_the_customer(self):
		self.set_mode(invoice_links.CHOSEN)
		self.assertIsNone(self.invoice.payment_link)
		frappe.db.set_value("Customer Group", "_Test Customer Group", "invoice_payment_links", "Yes")
		self.assertTrue(self.invoice.payment_link)
		frappe.db.set_value("Customer", "_Test Customer", "invoice_payment_links", "No")
		self.assertIsNone(self.invoice.payment_link)
		self.set_mode(invoice_links.OFF)
		frappe.db.set_value("Customer", "_Test Customer", "invoice_payment_links", "Yes")
		self.assertIsNone(self.invoice.payment_link)

	@patch(
		"erpnext.accounts.doctype.payment_request.payment_request.PaymentRequest.set_payment_request_url",
		set_payment_url,
	)
	def test_the_link_opens_a_payment_request_once_for_what_is_owed(self):
		first = invoice_links.payment_url(self.invoice)
		self.assertTrue(first.startswith(PAYMENT_URL))
		self.assertEqual(invoice_links.payment_url(self.invoice), first)
		request = frappe.get_doc("Payment Request", first.rsplit("=", 1)[1])
		self.assertEqual((request.docstatus, request.reference_name), (1, self.invoice.name))
		self.assertEqual(request.grand_total, self.invoice.outstanding_amount)

		# a part was paid some other way: the old request goes, a new one asks what is left
		frappe.db.set_value("Sales Invoice", self.invoice.name, "outstanding_amount", 400)
		self.invoice.reload()
		second = invoice_links.payment_url(self.invoice)
		self.assertNotEqual(second, first)
		self.assertEqual(frappe.db.get_value("Payment Request", request.name, "docstatus"), 2)
		self.assertEqual(frappe.db.get_value("Payment Request", second.rsplit("=", 1)[1], "grand_total"), 400)

	def test_the_page_goes_on_to_pay_or_says_why_not(self):
		from payments.templates.pages import pay_invoice

		key = invoice_links.key("Sales Invoice", self.invoice.name)
		frappe.local.form_dict = frappe._dict(invoice=self.invoice.name, key=key)
		with (
			patch.object(invoice_links, "payment_url", return_value=PAYMENT_URL),
			self.assertRaises(frappe.Redirect),
		):
			pay_invoice.get_context(frappe._dict())
		self.assertEqual(frappe.local.flags.redirect_location, PAYMENT_URL)

		frappe.local.form_dict = frappe._dict(invoice=self.invoice.name, key="0" * 32)
		self.assertEqual(pay_invoice.get_context(frappe._dict()).title, "Link not valid")

		frappe.local.form_dict = frappe._dict(
			customer="_Test Customer", key=invoice_links.key("Customer", "_Test Customer")
		)
		listed = pay_invoice.get_context(frappe._dict())
		self.assertIn(self.invoice.name, [row["name"] for row in listed.invoices])

	def test_invoice_emails_and_prints_carry_the_link(self):
		communication = frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"sent_or_received": "Sent",
				"subject": "Invoice",
				"content": "<p>Your invoice is attached.</p>",
				"reference_doctype": "Sales Invoice",
				"reference_name": self.invoice.name,
			}
		).insert(ignore_permissions=True)
		self.assertIn(self.invoice.payment_link.replace("&", "&amp;"), communication.content)
		# prints made with a template (Standard, or a Jinja print format)
		self.assertIn(
			"invoice-payment-link", frappe.get_print("Sales Invoice", self.invoice.name, "Standard")
		)

		self.set_mode(invoice_links.ALL, emails=0, prints=0)
		self.assertNotIn(
			"invoice-payment-link", frappe.get_print("Sales Invoice", self.invoice.name, "Standard")
		)

	def test_a_statement_can_link_to_the_customers_invoices(self):
		customer = frappe.get_doc("Customer", "_Test Customer")
		self.assertIn("customer=_Test", customer.payment_link)
		rendered = frappe.render_template(
			"{% if customer.payment_link %}Pay: {{ customer.payment_link }}{% endif %}",
			{"customer": customer},
			restrict_globals=True,
		)
		self.assertIn("/pay_invoice?customer=", rendered)

	def test_a_dunning_letter_links_to_the_overdue_invoices(self):
		from erpnext.accounts.doctype.dunning.test_dunning import create_dunning

		dunning = create_dunning(overdue_days=20)
		dunning.submit()
		self.assertIn("/pay_invoice?customer=", dunning.payment_link)
		communication = frappe.get_doc(
			{
				"doctype": "Communication",
				"communication_type": "Communication",
				"sent_or_received": "Sent",
				"subject": "Overdue",
				"content": "<p>Your account is overdue.</p>",
				"reference_doctype": "Dunning",
				"reference_name": dunning.name,
			}
		).insert(ignore_permissions=True)
		self.assertIn("Pay your overdue invoices online", communication.content)
		self.assertIn("invoice-payment-link", frappe.get_print("Dunning", dunning.name, "Standard"))
