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

	def make_payment_gateway(self, gateway_name="Test Gateway"):
		return frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": gateway_name,
				"payment_gateway_account": [],
			}
		)

	def insert_payment_gateway(self, gateway_name, is_default):
		doc = self.make_payment_gateway(gateway_name)
		doc.append(
			"payment_gateway_account",
			{"company": self.company_1, "payment_account": self.account_1, "is_default": is_default},
		)
		doc.insert(ignore_permissions=True)
		self.addCleanup(frappe.delete_doc, "Payment Gateway", doc.name, force=1)
		return doc

	def make_accounts_manager(self):
		email = "test_pg_accounts_manager@example.com"
		if not frappe.db.exists("User", email):
			user = frappe.new_doc("User")
			user.email = email
			user.first_name = "Accounts Manager"
			user.send_welcome_email = 0
			user.insert(ignore_permissions=True)
			user.add_roles("Accounts Manager")
		return email

	def test_accounts_manager_can_only_change_accounts(self):
		gateway = self.insert_payment_gateway("_Test PG Permissions", is_default=1)
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(self.make_accounts_manager())

		doc = frappe.get_doc("Payment Gateway", gateway.name)
		doc.gateway_settings = "Payment Gateway"
		doc.payment_gateway_account[0].message = "Pay now"
		doc.save()

		doc.reload()
		self.assertIsNone(doc.gateway_settings)
		self.assertEqual(doc.payment_gateway_account[0].message, "Pay now")
		self.assertFalse(frappe.has_permission("Payment Gateway", "create"))
		self.assertFalse(frappe.has_permission("Payment Gateway", "delete"))

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

	def test_first_account_is_default_when_company_has_none(self):
		doc = self.make_payment_gateway()

		doc.append(
			"payment_gateway_account",
			{
				"company": self.company_1,
				"payment_account": self.account_1,
				"is_default": 0,
			},
		)

		doc.validate()

		self.assertEqual(doc.payment_gateway_account[0].is_default, 1)

	def test_company_has_one_default_across_gateways(self):
		def default_gateways():
			return frappe.get_all(
				"Payment Gateway Account",
				filters={"parenttype": "Payment Gateway", "company": self.company_1, "is_default": 1},
				pluck="parent",
			)

		first = self.insert_payment_gateway("_Test PG First", is_default=1)
		second = self.insert_payment_gateway("_Test PG Second", is_default=0)
		self.assertEqual(default_gateways(), [first.name])

		second.payment_gateway_account[0].is_default = 1
		second.save(ignore_permissions=True)
		self.assertEqual(default_gateways(), [second.name])

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
