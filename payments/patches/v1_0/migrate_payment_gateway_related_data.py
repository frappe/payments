import frappe
from frappe.query_builder import DocType


def execute():
	if "erpnext" not in frappe.get_installed_apps():
		return

	from payments.utils import add_payment_gateway_account_child_columns, make_payments_erpnext_custom_fields

	add_payment_gateway_account_child_columns()
	make_payments_erpnext_custom_fields()

	PGA = DocType("Payment Gateway Account")

	update_pga_query = (
		frappe.qb.update(PGA)
		.set(PGA.parent, PGA.payment_gateway)
		.set(PGA.parenttype, "Payment Gateway")
		.set(PGA.parentfield, "payment_gateway_account")
		.where((PGA.parent.isnull() | (PGA.parent == "")) & PGA.payment_gateway.isnotnull())
	)
	update_pga_query.run()

	pg_list = frappe.get_all("Payment Gateway", pluck="name")

	for pg in pg_list:
		rows = frappe.get_all(
			"Payment Gateway Account", filters={"parent": pg}, pluck="name", order_by="creation asc"
		)
		for idx, row in enumerate(rows, start=1):
			frappe.db.set_value("Payment Gateway Account", row, "idx", idx, update_modified=False)

	sp_list = frappe.get_all(
		"Subscription Plan",
		fields=["name", "payment_gateway"],
	)

	for sp in sp_list:
		if sp.payment_gateway:
			doc_dict = frappe.db.get_value(
				"Payment Gateway Account",
				sp.payment_gateway,
				["parent", "payment_account"],
				as_dict=True,
			)

			if not doc_dict:
				continue

			frappe.db.set_value(
				"Subscription Plan",
				sp.name,
				{
					"payment_gateway": doc_dict.parent,
					"payment_account": doc_dict.payment_account,
				},
				update_modified=False,
			)

	# submitted requests already hold the right values in these columns
	pr_list = frappe.get_all(
		"Payment Request",
		filters={"docstatus": 0},
		fields=["name", "payment_gateway_account"],
	)

	for pr in pr_list:
		if pr.payment_gateway_account:
			doc_dict = frappe.db.get_value(
				"Payment Gateway Account",
				pr.payment_gateway_account,
				["parent", "payment_account", "payment_channel"],
				as_dict=True,
			)

			if not doc_dict:
				continue

			frappe.db.set_value(
				"Payment Request",
				pr.name,
				{
					"payment_gateway": doc_dict.parent,
					"payment_account": doc_dict.payment_account,
					"payment_channel": doc_dict.payment_channel,
				},
				update_modified=False,
			)
