# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import unittest

import frappe

# test_records = frappe.get_test_records('Payment Gateway')


class TestPaymentGateway(unittest.TestCase):
	@classmethod
	def setUpClass(cls):
		if "erpnext" not in frappe.get_installed_apps():
			raise unittest.SkipTest("ERPNext not installed")

		cls.company_1 = "_Test PG Company 1"
		cls.company_2 = "_Test PG Company 2"
		cls.account_1 = "_Test PG Bank 1 - _TP1"
		cls.account_2 = "_Test PG Bank 2 - _TP2"

		cls.created_company_1 = False
		cls.created_company_2 = False
		cls.created_account_1 = False
		cls.created_account_2 = False

		if not frappe.db.exists("Company", cls.company_1):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": cls.company_1,
					"abbr": "_TP1",
					"default_currency": "INR",
					"country": "India",
				}
			).insert(ignore_permissions=True)
			cls.created_company_1 = True

		if not frappe.db.exists("Company", cls.company_2):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": cls.company_2,
					"abbr": "_TP2",
					"default_currency": "INR",
					"country": "India",
				}
			).insert(ignore_permissions=True)
			cls.created_company_2 = True

		if not frappe.db.exists("Account", cls.account_1):
			parent_account = frappe.db.get_value(
				"Account",
				{"company": cls.company_1, "is_group": 1},
				"name",
			)

			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "_Test PG Bank 1",
					"company": cls.company_1,
					"parent_account": parent_account,
				}
			).insert(ignore_permissions=True)
			cls.created_account_1 = True

		if not frappe.db.exists("Account", cls.account_2):
			parent_account = frappe.db.get_value(
				"Account",
				{"company": cls.company_2, "is_group": 1},
				"name",
			)

			frappe.get_doc(
				{
					"doctype": "Account",
					"account_name": "_Test PG Bank 2",
					"company": cls.company_2,
					"parent_account": parent_account,
				}
			).insert(ignore_permissions=True)
			cls.created_account_2 = True

	@classmethod
	def tearDownClass(cls):
		if "erpnext" not in frappe.get_installed_apps():
			return

		if cls.created_account_1 and frappe.db.exists("Account", cls.account_1):
			frappe.delete_doc("Account", cls.account_1, force=1)

		if cls.created_account_2 and frappe.db.exists("Account", cls.account_2):
			frappe.delete_doc("Account", cls.account_2, force=1)

		if cls.created_company_1 and frappe.db.exists("Company", cls.company_1):
			frappe.delete_doc("Company", cls.company_1, force=1)

		if cls.created_company_2 and frappe.db.exists("Company", cls.company_2):
			frappe.delete_doc("Company", cls.company_2, force=1)

	def make_payment_gateway(self):
		return frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": "Test Gateway",
				"payment_gateway_account": [],
			}
		)

	def test_duplicate_company_payment_account_not_allowed(self):
		doc = self.make_payment_gateway()

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_1,
				"payment_account": self.account_1,
				"is_default": 1,
			},
		)

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_1,
				"payment_account": self.account_1,
				"is_default": 0,
			},
		)

		self.assertRaises(frappe.ValidationError, doc.validate)

	def test_default_required_per_company(self):
		doc = self.make_payment_gateway()

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_1,
				"payment_account": self.account_1,
				"is_default": 0,
			},
		)

		self.assertRaises(frappe.ValidationError, doc.validate)

	def test_only_one_default_per_company(self):
		doc = self.make_payment_gateway()

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_2,
				"payment_account": self.account_2,
				"is_default": 1,
			},
		)

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_2,
				"payment_account": self.account_2,
				"is_default": 1,
			},
		)

		self.assertRaises(frappe.ValidationError, doc.validate)

	def test_one_default_per_company_allowed(self):
		doc = self.make_payment_gateway()

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_1,
				"payment_account": self.account_1,
				"is_default": 1,
			},
		)

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_1,
				"payment_account": self.account_2,
				"is_default": 1,
			},
		)

		self.assertRaises(frappe.ValidationError, doc.validate)

	def test_child_sets_currency_from_account(self):
		doc = self.make_payment_gateway()

		row = doc.append(
			"payment_gateway_account",
			{
				"company": self.company_1,
				"payment_account": self.account_1,
				"is_default": 1,
			},
		)

		row.validate()

		self.assertEqual(
			doc.payment_gateway_account[0].currency,
			frappe.get_cached_value("Account", self.account_1, "account_currency"),
		)
