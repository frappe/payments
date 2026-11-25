import hashlib
import json

import frappe
from frappe import _
from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
from frappe.utils import flt


class NothingToPay(frappe.ValidationError):
	"""A document can't be paid: it is paid, cancelled or asks for nothing."""


def amount_to_pay(reference) -> tuple[float, str]:
	"""What a document still asks to be paid, in its currency."""
	if reference.docstatus != 1 or reference.get("status") in ("Paid", "Cancelled"):
		frappe.throw(
			_("{0} {1} has nothing left to pay").format(_(reference.doctype), reference.name),
			exc=NothingToPay,
		)
	amount = reference.get("grand_total")
	if reference.meta.has_field("outstanding_amount") and reference.get("outstanding_amount") is not None:
		amount = in_request_currency(reference, flt(reference.outstanding_amount))
	if flt(amount) <= 0:
		frappe.throw(
			_("{0} {1} has nothing left to pay").format(_(reference.doctype), reference.name),
			exc=NothingToPay,
		)
	return flt(amount), reference.get("currency")


def in_request_currency(reference, outstanding: float) -> float:
	"""What a Payment Request still asks, in its own currency: ERPNext keeps the outstanding amount
	in the currency of the party's account, which can differ from the request's."""
	party_currency = reference.get("party_account_currency")
	if not party_currency or party_currency == reference.get("currency"):
		return outstanding
	requested = requested_in_party_currency(reference)
	if not requested:
		return outstanding
	total = flt(reference.get("grand_total"))
	return min(flt(total * outstanding / requested, reference.precision("grand_total")), total)


def requested_in_party_currency(reference) -> float:
	"""The request's total in the currency of the party's account, as ERPNext works it out."""
	try:
		from erpnext.accounts.doctype.payment_request.payment_request import (
			get_requested_amount_in_party_account_currency,
		)
	except ImportError:
		return 0.0
	return flt(get_requested_amount_in_party_account_currency(reference))


def first_time(service: str, key: str, data: dict | None = None) -> bool:
	"""Whether a gateway's news (an event, or a refund or dispute in a given state) is new: it is
	logged under its key the first time."""
	if frappe.db.exists("Integration Request", {"integration_request_service": service, "request_id": key}):
		return False
	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": service,
			"request_id": key,
			"status": "Completed",
			"data": json.dumps(data or {}, default=str),
		}
	)
	# named after the key: the same news handled twice at once is logged, and so told, once
	log.flags._name = hashlib.sha256(f"{service}:{key}".encode()).hexdigest()
	frappe.db.savepoint("first_time")
	try:
		log.insert(ignore_permissions=True)
	except frappe.DuplicateEntryError:
		frappe.db.rollback(save_point="first_time")
		return False
	return True


def tell_staff(reference, note: str) -> None:
	"""Note something about a payment on its document (and the document that asks for it), and
	notify Accounts Managers."""
	documents = [reference]
	if reference.get("reference_doctype") and reference.get("reference_name"):
		documents.append(frappe.get_doc(reference.reference_doctype, reference.reference_name))
	for doc in documents:
		doc.add_comment("Info", note)
	staff = frappe.get_all(
		"Has Role",
		filters={
			"role": "Accounts Manager",
			"parenttype": "User",
			"parent": ["not in", ["Administrator", "Guest"]],
		},
		pluck="parent",
		distinct=True,
	)
	staff = [user for user in staff if frappe.db.get_value("User", user, "enabled")] or ["Administrator"]
	enqueue_create_notification(
		staff,
		{
			"type": "Alert",
			"document_type": documents[-1].doctype,
			"document_name": documents[-1].name,
			"subject": note,
			"from_user": "Administrator",
		},
	)
