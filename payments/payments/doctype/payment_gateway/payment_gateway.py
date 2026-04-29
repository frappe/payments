# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import frappe
from frappe.model.document import Document


class PaymentGateway(Document):
	def validate(self):
		self.validate_default_pga()

	def validate_default_pga(self):
		if not hasattr(self, "payment_gateway_account"):
			return

		if not self.payment_gateway_account:
			return

		defaults = [row for row in self.payment_gateway_account if row.is_default]

		if len(defaults) == 0:
			frappe.throw("At least one Payment Gateway Account must be set as default")

		if len(defaults) > 1:
			frappe.throw("Only one Payment Gateway Account can be set as default")
