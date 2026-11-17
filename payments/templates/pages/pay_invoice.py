# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
"""Where an invoice's payment link leads: on to its payment page; or, from a statement, the
customer's unpaid invoices with a link each. See payments.utils.invoice_links."""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit
from frappe.utils import fmt_money

from payments.utils import invoice_links

no_cache = 1


def get_context(context):
	form = frappe.form_dict
	if not invoice_links.erpnext_installed():
		raise frappe.PageDoesNotExistError
	if form.get("invoice"):
		return open_invoice(context, form.invoice, form.get("key"))
	if form.get("customer"):
		return list_invoices(context, form.customer, form.get("key"))
	raise frappe.PageDoesNotExistError


@rate_limit(limit=30, seconds=10 * 60)
def open_invoice(context, name: str, key: str | None):
	if not invoice_links.valid_key("Sales Invoice", name, key) or not frappe.db.exists("Sales Invoice", name):
		return message(
			context, _("Link not valid"), _("This payment link isn't valid. Please ask us for a new one.")
		)
	invoice = frappe.get_doc("Sales Invoice", name)
	if invoice.docstatus == 2:
		return message(
			context,
			_("Invoice cancelled"),
			_("Invoice {0} was cancelled, so there's nothing to pay.").format(name),
		)
	if invoice.outstanding_amount <= 0:
		return message(context, _("Invoice paid"), _("Invoice {0} is paid. Thank you.").format(name), "green")
	if not invoice_links.payable(invoice):
		return message(
			context,
			_("Online payment not available"),
			_("Invoice {0} can't be paid online. Please pay it as the invoice says.").format(name),
		)
	frappe.local.flags.redirect_location = invoice_links.payment_url(invoice)
	# the Payment Request made here is kept, though this is a GET
	frappe.local.flags.commit = True
	raise frappe.Redirect(302)


@rate_limit(limit=30, seconds=10 * 60)
def list_invoices(context, customer: str, key: str | None):
	if not invoice_links.valid_key("Customer", customer, key) or not frappe.db.exists("Customer", customer):
		return message(
			context, _("Link not valid"), _("This payment link isn't valid. Please ask us for a new one.")
		)
	context.customer_name = frappe.utils.escape_html(
		frappe.db.get_value("Customer", customer, "customer_name")
	)
	context.invoices = [
		{
			"name": invoice.name,
			"due_date": frappe.utils.formatdate(invoice.due_date) if invoice.due_date else None,
			"owed": fmt_money(invoice.outstanding_amount, currency=invoice.currency),
			"link": invoice.payment_link,
		}
		for invoice in invoice_links.unpaid_invoices(customer)
	]
	context.title = _("Pay your invoices")
	return context


def message(context, title: str, text: str, color: str = "orange"):
	context.title = title
	context.message = text
	context.indicator = color
	return context
