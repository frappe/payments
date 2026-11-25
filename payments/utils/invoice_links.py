"""Payment links on ERPNext Sales Invoices: a private link that opens the invoice's payment page,
in its emails and on its prints, for the customers Accounts Settings says.

Accounts Settings > Invoice Payment Links is "All Customers", "Chosen Customers" or "Off"; a
Customer, or failing that its Customer Group, can say "Yes" or "No" to override. A link is only
given for an unpaid invoice in a currency a Payment Gateway Account takes. Opening it makes (or
reuses) the invoice's Payment Request, and goes on to its gateway's page, so it can be paid by
anyone holding the link, without signing in.

Other apps use `payment_link` (Sales Invoice and Customer, see InvoiceLinks and CustomerLinks):
a statement email's body can say {{ customer.payment_link }}."""

import hashlib
import hmac
from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.utils import flt, get_url

ALL = "All Customers"
CHOSEN = "Chosen Customers"
OFF = "Off"
PAGE = "/pay_invoice"
PRINT_HOOK = "payments.utils.invoice_links.pdf_body_html"
# documents a customer is sent that carry a payment link
LINKED = ("Sales Invoice", "Dunning")


def erpnext_installed() -> bool:
	return "erpnext" in frappe.get_installed_apps()


def settings() -> frappe._dict:
	"""Accounts Settings' payment link fields; one never saved has its default (links on, in emails
	and prints)."""
	defaults = {"invoice_payment_links": ALL, "payment_link_in_emails": 1, "payment_link_in_prints": 1}
	# what was saved, as get_single_value reads an unsaved Check as 0
	saved = dict(
		frappe.db.sql(
			"select field, value from `tabSingles` where doctype = %s and field in %s",
			("Accounts Settings", tuple(defaults)),
		)
	)
	values = frappe._dict()
	for field, default in defaults.items():
		value = saved.get(field)
		if value in (None, ""):
			values[field] = default
		else:
			values[field] = value if field == "invoice_payment_links" else frappe.utils.cint(value)
	if not erpnext_installed():
		values.invoice_payment_links = OFF
	return values


def customer_wants_links(customer: str) -> bool:
	mode = settings().invoice_payment_links
	if mode == OFF or not customer:
		return False
	choice = frappe.db.get_value("Customer", customer, "invoice_payment_links")
	if not choice:
		group = frappe.db.get_value("Customer", customer, "customer_group")
		choice = group and frappe.db.get_value("Customer Group", group, "invoice_payment_links")
	if choice:
		return choice == "Yes"
	return mode == ALL


def gateway_account(company: str, currency: str) -> str | None:
	"""The Payment Gateway Account that takes payments in this currency for the company, the
	default one first."""
	return frappe.db.get_value(
		"Payment Gateway Account",
		{"company": company, "currency": currency},
		"name",
		order_by="is_default desc, creation asc",
	)


def payable(invoice) -> bool:
	"""An invoice that can be paid online through a link."""
	return bool(
		invoice.docstatus == 1
		and not invoice.get("is_return")
		and flt(invoice.outstanding_amount) > 0
		and customer_wants_links(invoice.customer)
		and gateway_account(invoice.company, invoice.currency)
	)


def key(doctype: str, name: str) -> str:
	"""A key only this site can make, so a link can't be guessed from an invoice's name."""
	from frappe.utils.password import get_encryption_key

	message = f"{doctype}:{name}".encode()
	return hmac.new(get_encryption_key().encode(), message, hashlib.sha256).hexdigest()[:32]


def valid_key(doctype: str, name: str, given: str | None) -> bool:
	return bool(given) and hmac.compare_digest(key(doctype, name), given)


def invoice_link(invoice) -> str | None:
	if not payable(invoice):
		return None
	return get_url(
		f"{PAGE}?{urlencode({'invoice': invoice.name, 'key': key('Sales Invoice', invoice.name)})}"
	)


def customer_link(customer: str) -> str | None:
	"""The page listing the customer's unpaid invoices, each with its own payment link."""
	if not customer_wants_links(customer) or not unpaid_invoices(customer):
		return None
	return get_url(f"{PAGE}?{urlencode({'customer': customer, 'key': key('Customer', customer)})}")


def unpaid_invoices(customer: str) -> list:
	names = frappe.get_all(
		"Sales Invoice",
		filters={"customer": customer, "docstatus": 1, "is_return": 0, "outstanding_amount": [">", 0]},
		pluck="name",
		order_by="due_date asc, posting_date asc",
	)
	return [
		invoice for invoice in (frappe.get_doc("Sales Invoice", name) for name in names) if payable(invoice)
	]


class InvoiceLinks:
	"""Sales Invoice: the link a customer pays it through."""

	@property
	def payment_link(self) -> str | None:
		return invoice_link(self)


class DunningLinks:
	"""Dunning: the customer's unpaid invoices to pay (a Payment Request can't be made against a
	Dunning, so its letter links to each invoice's payment)."""

	@property
	def payment_link(self) -> str | None:
		return customer_link(self.customer) if self.docstatus != 2 else None


class CustomerLinks:
	"""Customer: the page with their unpaid invoices to pay, for statements."""

	@property
	def payment_link(self) -> str | None:
		return customer_link(self.name)


def payment_url(invoice) -> str:
	"""The gateway page for what the invoice still owes: its open Payment Request's, else a new
	one's (an open one asking a different amount, after a part payment, is cancelled first)."""
	from erpnext.accounts.doctype.payment_request.payment_request import (
		make_payment_request,
		run_as_administrator,
	)

	# two clicks at once make one request
	frappe.db.get_value("Sales Invoice", invoice.name, "name", for_update=True)
	owed = flt(invoice.outstanding_amount, invoice.precision("outstanding_amount"))
	# a payer who isn't signed in has no Desk permissions; callers check the link's key first
	with run_as_administrator():
		for row in frappe.get_all(
			"Payment Request",
			filters={
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice.name,
				"docstatus": 1,
				"status": ["in", ["Initiated", "Requested"]],
			},
			fields=["name", "grand_total", "payment_url"],
			order_by="creation desc",
		):
			if flt(row.grand_total, 2) == flt(owed, 2) and row.payment_url:
				return row.payment_url
			frappe.get_doc("Payment Request", row.name).cancel()
		request = make_payment_request(
			dt="Sales Invoice",
			dn=invoice.name,
			payment_gateway_account=gateway_account(invoice.company, invoice.currency),
			recipient_id=invoice.contact_email,
			mute_email=1,
			submit_doc=1,
			return_doc=1,
		)
	return request.payment_url


def add_link_to_email(doc, method=None) -> None:
	"""An invoice, or a Dunning letter, emailed to the customer carries its payment link (Accounts
	Settings)."""
	if not (
		erpnext_installed()
		and doc.reference_doctype in LINKED
		and doc.communication_type == "Communication"
		and doc.sent_or_received == "Sent"
		and settings().payment_link_in_emails
	):
		return
	reference = frappe.get_doc(doc.reference_doctype, doc.reference_name)
	link = reference.payment_link
	if link and link not in (doc.content or ""):
		doc.content = (doc.content or "") + link_html(reference, link)


def pdf_body_html(template, args, **kwargs):
	"""A printed invoice (PDF or print view) carries its payment link (Accounts Settings). Wraps
	the print renderer installed before this one."""
	hooks = frappe.get_hooks("pdf_body_html")
	position = hooks.index(PRINT_HOOK) if PRINT_HOOK in hooks else len(hooks)
	previous = hooks[position - 1] if position else "frappe.utils.pdf.pdf_body_html"
	html = frappe.get_attr(previous)(template=template, args=args, **kwargs)
	doc = args.get("doc")
	if not (
		erpnext_installed() and getattr(doc, "doctype", None) in LINKED and settings().payment_link_in_prints
	):
		return html
	link = doc.payment_link
	if not link:
		return html
	block = link_html(doc, link, printed=True)
	closing = html.rfind("</body>")
	return html[:closing] + block + html[closing:] if closing != -1 else html + block


def link_html(doc, link: str, printed: bool = False) -> str:
	esc = frappe.utils.escape_html
	label = (
		_("Pay your overdue invoices online:")
		if doc.doctype == "Dunning"
		else _("Pay invoice {0} online:").format(doc.name)
	)
	style = (
		"margin-top:16px;padding:10px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;"
		if printed
		else "margin-top:16px;"
	)
	return (
		f'<div class="invoice-payment-link" style="{style}">'
		f"{esc(label)} "
		f'<a href="{esc(link)}">{esc(link if printed else _("Pay now"))}</a></div>'
	)


FIELDS = {
	"Accounts Settings": [
		{
			"fieldname": "invoice_payment_links_section",
			"fieldtype": "Section Break",
			"label": "Invoice Payment Links",
			"insert_after": "make_payment_via_journal_entry",
			"collapsible": 1,
		},
		{
			"fieldname": "invoice_payment_links",
			"fieldtype": "Select",
			"label": "Invoice Payment Links",
			"options": f"{ALL}\n{CHOSEN}\n{OFF}",
			"default": ALL,
			"insert_after": "invoice_payment_links_section",
			"description": "A link to pay an unpaid invoice online, for invoices in a currency a Payment "
			"Gateway Account takes. Chosen Customers: only customers, or Customer Groups, set to Yes.",
		},
		{
			"fieldname": "payment_link_in_emails",
			"fieldtype": "Check",
			"label": "Add the Link to Invoice Emails",
			"default": "1",
			"insert_after": "invoice_payment_links",
			"depends_on": f"eval:doc.invoice_payment_links != '{OFF}'",
		},
		{
			"fieldname": "payment_link_in_prints",
			"fieldtype": "Check",
			"label": "Show the Link on Printed Invoices",
			"default": "1",
			"insert_after": "payment_link_in_emails",
			"description": "Added to invoices printed with the Standard or a Jinja print format. Print formats "
			"made with the print format builder (such as Sales Invoice Classic) show it when the letter "
			'head\'s footer says {% if doc.payment_link %}<a href="{{ doc.payment_link }}">Pay online</a>{% endif %}.',
			"depends_on": f"eval:doc.invoice_payment_links != '{OFF}'",
		},
	],
	**{
		doctype: [
			{
				"fieldname": "invoice_payment_links",
				"fieldtype": "Select",
				"label": "Invoice Payment Links",
				"options": "\nYes\nNo",
				"insert_after": after,
				"description": "Empty follows "
				+ ("the Customer Group, then " if doctype == "Customer" else "")
				+ "Accounts Settings.",
			}
		]
		for doctype, after in (("Customer Group", "default_price_list"), ("Customer", "default_price_list"))
	},
}


def make_fields() -> None:
	"""The settings above, on ERPNext's own documents (run after install and every migrate)."""
	if not erpnext_installed():
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	create_custom_fields(FIELDS, ignore_validate=True)


def delete_fields() -> None:
	for doctype, fields in FIELDS.items():
		frappe.db.delete(
			"Custom Field", {"dt": doctype, "fieldname": ["in", [f["fieldname"] for f in fields]]}
		)


@frappe.whitelist()
def get_invoice_payment_link(invoice: str) -> str | None:
	"""For staff copying an invoice's payment link (the Sales Invoice form)."""
	doc = frappe.get_doc("Sales Invoice", invoice)
	doc.check_permission("read")
	return doc.payment_link
