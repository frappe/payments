# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.model.document import Document


class PaymentGateway(Document):
	def validate(self):
		self.validate_duplicate_pga_entries()
		self.validate_default_pga()

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

	def validate_default_pga(self):
		if not hasattr(self, "payment_gateway_account"):
			return

		if not self.payment_gateway_account:
			return

		company_defaults = {}

		for row in self.payment_gateway_account:
			company_defaults.setdefault(row.company, 0)

			if row.is_default:
				company_defaults[row.company] += 1

		for company, count in company_defaults.items():
			if count == 0:
				frappe.throw(
					_("At least one Payment Gateway Account must be set as default for company {0}").format(
						company
					)
				)

			if count > 1:
				frappe.throw(
					_("Only one Payment Gateway Account can be set as default for company {0}").format(
						company
					)
				)

	def get_account_currency(self, row):
		if not row.payment_account:
			return None
		return frappe.get_cached_value("Account", row.payment_account, "account_currency")
