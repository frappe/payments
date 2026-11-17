# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.utils import payment_request


class TestPaymentRequestHelpers(IntegrationTestCase):
	def test_what_is_owed_is_in_the_requests_currency(self):
		# a USD request for a customer whose account is in INR: ERPNext keeps what is owed in INR
		values = {
			"status": "Requested",
			"currency": "USD",
			"party_account_currency": "INR",
			"grand_total": 60.12,
			"outstanding_amount": 5011.00,
		}
		request = MagicMock(docstatus=1, outstanding_amount=5011.00)
		request.get.side_effect = lambda field, default=None: values.get(field, default)
		request.meta.has_field.return_value = True
		request.precision.return_value = 2
		with patch.object(payment_request, "requested_in_party_currency", return_value=5011.00):
			self.assertEqual(payment_request.amount_to_pay(request), (60.12, "USD"))
			# half paid
			values["outstanding_amount"] = request.outstanding_amount = 2505.50
			self.assertEqual(payment_request.amount_to_pay(request), (30.06, "USD"))
		values.update(party_account_currency="USD", outstanding_amount=60.12)
		request.outstanding_amount = 60.12
		self.assertEqual(payment_request.amount_to_pay(request), (60.12, "USD"))

	def test_a_paid_or_cancelled_document_has_nothing_to_pay(self):
		for docstatus, status in ((1, "Paid"), (2, "Cancelled"), (0, "Draft")):
			request = MagicMock(docstatus=docstatus)
			request.get.side_effect = {"status": status}.get
			with self.assertRaises(payment_request.NothingToPay):
				payment_request.amount_to_pay(request)

	def test_news_is_new_once(self):
		key = f"_test-{frappe.generate_hash(length=8)}"
		self.assertTrue(payment_request.first_time("_Test Gateway", key))
		self.assertFalse(payment_request.first_time("_Test Gateway", key))
		self.assertTrue(payment_request.first_time("_Test Gateway", f"{key}:closed"))
		# sent twice at once, both see it unlogged: only one logs it
		with patch.object(payment_request.frappe.db, "exists", return_value=False):
			self.assertFalse(payment_request.first_time("_Test Gateway", key))
		self.assertTrue(frappe.db.exists("Integration Request", {"request_id": f"{key}:closed"}))

	def test_staff_are_told_on_the_document_and_the_one_it_asks_for(self):
		order = MagicMock(doctype="Sales Order")
		order.name = "SO-1"
		request = MagicMock(doctype="Payment Request", reference_doctype="Sales Order", reference_name="SO-1")
		request.name = "PR-1"
		request.get.side_effect = {"reference_doctype": "Sales Order", "reference_name": "SO-1"}.get
		with (
			patch("frappe.get_doc", return_value=order),
			patch("frappe.db.get_value", return_value=1),
			patch.object(payment_request.frappe, "get_all", return_value=["accounts@example.com"]),
			patch.object(payment_request, "enqueue_create_notification") as notify,
		):
			payment_request.tell_staff(request, "A refund was made")
			# two notices of the same words are two notices
			payment_request.tell_staff(request, "A refund was made")
		self.assertEqual(request.add_comment.call_count, 2)
		self.assertEqual(order.add_comment.call_count, 2)
		self.assertEqual(notify.call_args.args[0], ["accounts@example.com"])
		self.assertEqual(notify.call_args.args[1]["document_name"], "SO-1")
