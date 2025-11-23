# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import frappe

no_cache = True


def get_context(context):
	context.payment_message = ""
	friendly_message = (
		"Your payment has been successfully completed. However, we were unable to load the confirmation page due to a technical issue. Please contact our support team for verification."
	)

	try:
		token = frappe.local.form_dict.get("token")

		ref = None
		doc = None

		if token:
			ref = frappe.cache().get_value(f"payment_success:{token}")
		
		if ref:
			doctype = ref.get("doctype")
			docname = ref.get("docname")

			doc = frappe.get_doc(doctype, docname)

			frappe.cache().delete_key(f"payment_success:{token}")
		else:
			doctype = frappe.local.form_dict.doctype
			docname = frappe.local.form_dict.docname

			if doctype and docname:
				doc = frappe.get_doc(doctype, docname)
				
			else:
				context.payment_message = friendly_message
				return context

		if hasattr(doc, "get_payment_success_message"):
			context.payment_message = doc.get_payment_success_message()
		else:
			context.payment_message = friendly_message

	except Exception as e:
		frappe.log_error(frappe.get_traceback(), "Payment Success Page Error")

		context.payment_message = friendly_message

	return context
