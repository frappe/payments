# Copyright (c) 2026, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class PaymentGatewayAccount(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		company: DF.Link
		currency: DF.ReadOnly | None
		is_default: DF.Check
		message: DF.SmallText | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		payment_account: DF.Link
		payment_channel: DF.Literal["", "Email", "Phone", "Other"]
	# end: auto-generated types

	def validate(self):
		if "erpnext" in frappe.get_installed_apps() and self.payment_account:
			self.currency = frappe.get_cached_value("Account", self.payment_account, "account_currency")
