# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document


class PaymentGateway(Document):
	def validate(self):
		self.validate_duplicate_pga_entries()
		self.keep_one_default_per_company()
		self.set_company_defaults()

	def validate_duplicate_pga_entries(self):
		if not hasattr(self, "payment_gateway_account"):
			return

		unique_pairs = set()

		for row in self.payment_gateway_account:
			currency = self.get_account_currency(row)
			key = (row.company, currency)

			if key in unique_pairs:
				frappe.throw(
					_("Duplicate entry: {} - {} is already added under this Payment Gateway").format(
						row.company, currency
					)
				)

			unique_pairs.add(key)

	def keep_one_default_per_company(self):
		if not hasattr(self, "payment_gateway_account"):
			return

		# as on develop, the newly ticked default replaces the old one
		before = self.get_doc_before_save()
		was_default = set()
		if before:
			was_default = {row.name for row in before.payment_gateway_account if row.is_default}

		defaults_by_company = {}
		for row in self.payment_gateway_account:
			if row.is_default:
				defaults_by_company.setdefault(row.company, []).append(row)

		for rows in defaults_by_company.values():
			keep = next((row for row in rows if row.name not in was_default), rows[-1])
			for row in rows:
				row.is_default = int(row is keep)

	def set_company_defaults(self):
		# one default per company across gateways, and the default row also decides the gateway
		if not hasattr(self, "payment_gateway_account"):
			return

		rows_by_company = {}
		for row in self.payment_gateway_account:
			rows_by_company.setdefault(row.company, []).append(row)

		for company, rows in rows_by_company.items():
			default = next((row for row in rows if row.is_default), None)
			if default:
				default.make_only_default_of_company()
			elif not self.company_has_default_elsewhere(company):
				rows[0].is_default = 1

	def company_has_default_elsewhere(self, company):
		return frappe.db.exists(
			"Payment Gateway Account",
			{
				"parenttype": "Payment Gateway",
				"company": company,
				"is_default": 1,
				"parent": ("!=", self.name or ""),
			},
		)

	def get_account_currency(self, row):
		if not row.payment_account:
			return None
		return frappe.get_cached_value("Account", row.payment_account, "account_currency")
