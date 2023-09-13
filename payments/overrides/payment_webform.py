import json

import frappe
from frappe.core.doctype.file.utils import remove_file_by_url
from frappe.rate_limiter import rate_limit
from frappe.utils import flt
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
		elif not self.amount_based_on_field and not flt(self.amount) > 0:
			frappe.throw(frappe._("Amount must be greater than 0."))

	def get_payment_gateway_url(self, doc):
		if getattr(self, "accept_payment", False):
			controller = get_payment_gateway_controller(self.payment_gateway)

			title = f"Payment for {doc.doctype} {doc.name}"
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
				"redirect_to": frappe.utils.get_url(self.success_url or self.route),
			}

			# Redirect the user to this url
			return controller.get_payment_url(**payment_details)


@frappe.whitelist(allow_guest=True)
@rate_limit(key="web_form", limit=5, seconds=60, methods=["POST"])
def accept(web_form, data, docname=None, for_payment=False):
	"""Save the web form"""
	data = frappe._dict(json.loads(data))
	for_payment = frappe.parse_json(for_payment)

	docname = docname or data.name

	files = []
	files_to_delete = []

	web_form = frappe.get_doc("Web Form", web_form)

	if docname and not web_form.allow_edit:
		frappe.throw(frappe._("You are not allowed to update this Web Form Document"))

	frappe.flags.in_web_form = True
	meta = frappe.get_meta(data.doctype)

	if docname:
		# update
		doc = frappe.get_doc(data.doctype, docname)
	else:
		# insert
		doc = frappe.new_doc(data.doctype)

	# set values
	for field in web_form.web_form_fields:
		fieldname = field.fieldname
		df = meta.get_field(fieldname)
		value = data.get(fieldname, None)

		if df and df.fieldtype in ("Attach", "Attach Image"):
			if value and "data:" and "base64" in value:
				files.append((fieldname, value))
				if not doc.name:
					doc.set(fieldname, "")
				continue

			elif not value and doc.get(fieldname):
				files_to_delete.append(doc.get(fieldname))

		doc.set(fieldname, value)

	if for_payment:
		web_form.validate_mandatory(doc)
		doc.run_method("validate_payment")

	if doc.name:
		if web_form.has_web_form_permission(doc.doctype, doc.name, "write"):
			doc.save(ignore_permissions=True)
		else:
			# only if permissions are present
			doc.save()

	else:
		# insert
		if web_form.login_required and frappe.session.user == "Guest":
			frappe.throw(frappe._("You must login to submit this form"))

		ignore_mandatory = True if files else False

		doc.insert(ignore_permissions=True, ignore_mandatory=ignore_mandatory)

	# add files
	if files:
		for f in files:
			fieldname, filedata = f

			# remove earlier attached file (if exists)
			if doc.get(fieldname):
				remove_file_by_url(doc.get(fieldname), doctype=doc.doctype, name=doc.name)

			# save new file
			filename, dataurl = filedata.split(",", 1)
			_file = frappe.get_doc(
				{
					"doctype": "File",
					"file_name": filename,
					"attached_to_doctype": doc.doctype,
					"attached_to_name": doc.name,
					"content": dataurl,
					"decode": True,
				}
			)
			_file.save()

			# update values
			doc.set(fieldname, _file.file_url)

		doc.save(ignore_permissions=True)

	if files_to_delete:
		for f in files_to_delete:
			if f:
				remove_file_by_url(f, doctype=doc.doctype, name=doc.name)

	frappe.flags.web_form_doc = doc

	if for_payment:
		# this is needed for Payments app
		return web_form.get_payment_gateway_url(doc)
	else:
		return doc
