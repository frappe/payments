from decimal import Decimal

import frappe
from frappe.utils import flt
from frappe.website.doctype.web_form.web_form import WebForm
from frappe.website.doctype.web_form.web_form import accept as frappe_web_form_accept

from payments.utils import get_payment_gateway_controller


class PaymentWebForm(WebForm):
	def validate(self):
		super().validate()

		if getattr(self, "accept_payment", False):
			self.validate_payment_amount()

	def validate_submission(self, doc, uploaded_fields=None):
		super().validate_submission(doc, uploaded_fields=uploaded_fields)

		if self.get("accept_payment") and not doc.get("paid"):
			doc.run_method("validate_payment")

	def validate_payment_amount(self):
		if self.amount_based_on_field and not self.amount_field:
			frappe.throw(frappe._("Please select a Amount Field."))
		elif not self.amount_based_on_field and not flt(self.amount) > 0:
			frappe.throw(frappe._("Amount must be greater than 0."))

	def get_payment_gateway_url(self, doc):
		if getattr(self, "accept_payment", False):
			controller = get_payment_gateway_controller(self.payment_gateway)

			title = f"Payment for {doc.doctype} {doc.name}"
			amount = self.amount
			if self.amount_based_on_field:
				amount = doc.get(self.amount_field)

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
				"redirect_to": frappe.utils.get_url(self.success_url or self.route),
				"payment_gateway": self.payment_gateway
			}

			# Redirect the user to this url
			return controller.get_payment_url(**payment_details)


@frappe.whitelist(  # nosemgrep: frappe-semgrep-rules.rules.security.guest-whitelisted-method
	methods=["POST", "PUT"], allow_guest=True
)
def accept(
	web_form: str,
	data: str | dict,
	web_form_request_key: str | None = None,
	for_payment: bool = False,
):
	"""Save through Frappe's Web Form handler and add the payment redirect when configured."""
	response_doc = frappe_web_form_accept(
		web_form=web_form,
		data=data,
		web_form_request_key=web_form_request_key,
	)
	payment_web_form = frappe.get_cached_doc("Web Form", web_form)
	saved_doc = frappe.flags.web_form_doc

	# Keep accepting for_payment for browser/API compatibility, but do not trust it
	# to decide whether a configured payment can be skipped.
	if not payment_web_form.get("accept_payment") or saved_doc.get("paid"):
		return response_doc

	return payment_web_form.get_payment_gateway_url(saved_doc)
