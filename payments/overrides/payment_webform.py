import frappe
from frappe.website.doctype.web_form.web_form import WebForm

from payments.utils import get_payment_gateway_controller


class PaymentWebForm(WebForm):
	def validate(self):
		super().validate()

		if getattr(self, "accept_payment", False):
			self.validate_payment_amount()

	def validate_payment_amount(self):
		if self.amount_based_on_field and not self.amount_field:
			frappe.throw(frappe._("Please select a Amount Field."))
		elif not self.amount_based_on_field and not self.amount > 0:
			frappe.throw(frappe._("Amount must be greater than 0."))

	def get_payment_gateway_url(self, doc):
		if getattr(self, "accept_payment", False):
			controller = get_payment_gateway_controller(self.payment_gateway)

			title = "Payment for {0} {1}".format(doc.doctype, doc.name)
			amount = self.amount
			if self.amount_based_on_field:
				amount = doc.get(self.amount_field)

			from decimal import Decimal
			if amount is None or Decimal(amount) <= 0:
				return frappe.utils.get_url(self.success_url or self.route)

			payment_details = {
				"amount": amount,
				"title": title,
				"description": title,
				"reference_doctype": doc.doctype,
				"reference_docname": doc.name,
				"payer_email": frappe.session.user,
				"payer_name": frappe.utils.get_fullname(frappe.session.user),
				"order_id": doc.name,
				"currency": self.currency,
				"redirect_to": frappe.utils.get_url(self.success_url or self.route)
			}

			# Redirect the user to this url
			return controller.get_payment_url(**payment_details)
