from contextlib import contextmanager
from importlib import import_module

import click
import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def validate_integration_request(docname: str | None):
	if frappe.db.get_value("Integration Request", docname, "status") == "Cancelled":
		frappe.throw(_("Expired Token"))


def get_payment_gateway_controller(payment_gateway):
	"""Return payment gateway controller"""
	gateway = frappe.get_doc("Payment Gateway", payment_gateway)
	if gateway.gateway_controller is None:
		try:
			return frappe.get_doc(f"{payment_gateway} Settings")
		except Exception:
			frappe.throw(_("{0} Settings not found").format(payment_gateway))
	else:
		try:
			return frappe.get_doc(gateway.gateway_settings, gateway.gateway_controller)
		except Exception:
			frappe.throw(_("{0} Settings not found").format(payment_gateway))


@frappe.whitelist(allow_guest=True, xss_safe=True)
def get_checkout_url(**kwargs):
	try:
		if kwargs.get("payment_gateway"):
			doc = frappe.get_doc("{} Settings".format(kwargs.get("payment_gateway")))
			return doc.get_payment_url(**kwargs)
		else:
			raise Exception
	except Exception:
		frappe.respond_as_web_page(
			_("Something went wrong"),
			_(
				"Looks like something is wrong with this site's payment gateway configuration. No payment has been made."
			),
			indicator_color="red",
			http_status_code=frappe.ValidationError.http_status_code,
		)


def create_payment_gateway(gateway, settings=None, controller=None):
	# NOTE: we don't translate Payment Gateway name because it is an internal doctype
	if not frappe.db.exists("Payment Gateway", gateway):
		payment_gateway = frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway_name": gateway,
				"gateway_settings": settings,
				"gateway_controller": controller,
			}
		)
		payment_gateway.insert(ignore_permissions=True)


def after_install():
	make_custom_fields()
	make_payments_erpnext_custom_fields()


def before_uninstall():
	delete_custom_fields()
	delete_payments_erpnext_custom_fields()


def make_custom_fields():
	if not frappe.get_meta("Web Form").has_field("payments_tab"):
		click.secho("* Installing Payment Custom Fields in Web Form")

		create_custom_fields(
			{
				"Web Form": [
					{
						"fieldname": "payments_tab",
						"fieldtype": "Tab Break",
						"label": "Payments",
						"insert_after": "custom_css",
					},
					{
						"default": "0",
						"fieldname": "accept_payment",
						"fieldtype": "Check",
						"label": "Accept Payment",
						"insert_after": "payments",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "payment_gateway",
						"fieldtype": "Link",
						"label": "Payment Gateway",
						"options": "Payment Gateway",
						"insert_after": "accept_payment",
					},
					{
						"default": "Buy Now",
						"depends_on": "accept_payment",
						"fieldname": "payment_button_label",
						"fieldtype": "Data",
						"label": "Button Label",
						"insert_after": "payment_gateway",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "payment_button_help",
						"fieldtype": "Text",
						"label": "Button Help",
						"insert_after": "payment_button_label",
					},
					{
						"fieldname": "payments_cb",
						"fieldtype": "Column Break",
						"insert_after": "payment_button_help",
					},
					{
						"default": "0",
						"depends_on": "accept_payment",
						"fieldname": "amount_based_on_field",
						"fieldtype": "Check",
						"label": "Amount Based On Field",
						"insert_after": "payments_cb",
					},
					{
						"depends_on": "eval:doc.accept_payment && doc.amount_based_on_field",
						"fieldname": "amount_field",
						"fieldtype": "Select",
						"label": "Amount Field",
						"insert_after": "amount_based_on_field",
					},
					{
						"depends_on": "eval:doc.accept_payment && !doc.amount_based_on_field",
						"fieldname": "amount",
						"fieldtype": "Currency",
						"label": "Amount",
						"insert_after": "amount_field",
					},
					{
						"depends_on": "accept_payment",
						"fieldname": "currency",
						"fieldtype": "Link",
						"label": "Currency",
						"options": "Currency",
						"insert_after": "amount",
					},
				]
			}
		)

		frappe.clear_cache(doctype="Web Form")


def make_payments_erpnext_custom_fields():
	apps = frappe.get_installed_apps()
	if "erpnext" not in apps or "payments" not in apps:
		return

	for doctype in get_payments_erpnext_custom_fields():
		click.secho(f"* Installing Payments Custom Fields in {doctype}")

	create_custom_fields(get_payments_erpnext_custom_fields())


def get_payments_erpnext_custom_fields():
	return {
		"GoCardless Mandate": [
			{
				"fieldname": "customer",
				"fieldtype": "Link",
				"in_list_view": 1,
				"label": "Customer",
				"options": "Customer",
				"reqd": 1,
				"insert_after": "disabled",
			}
		],
		"Payment Gateway Account": [
			{
				"fieldname": "company",
				"fieldtype": "Link",
				"in_list_view": 1,
				"label": "Company",
				"options": "Company",
				"reqd": 1,
				"insert_after": "section_break_1",
			},
			{
				"fieldname": "payment_account",
				"fieldtype": "Link",
				"in_list_view": 1,
				"label": "Payment Account",
				"options": "Account",
				"reqd": 1,
				"insert_after": "company",
			},
		],
		"Payment Gateway": [
			{
				"fieldname": "pga_section",
				"fieldtype": "Section Break",
				"insert_after": "gateway_controller",
			},
			{
				"fieldname": "payment_gateway_account",
				"fieldtype": "Table",
				"label": "Payment Gateway Account",
				"options": "Payment Gateway Account",
				"insert_after": "pga_section",
			},
		],
		"Subscription Plan": [
			{
				"fieldname": "payment_gateway",
				"fieldtype": "Link",
				"label": "Payment Gateway",
				"options": "Payment Gateway",
				"insert_after": "column_break_16",
			},
			{
				"fieldname": "payment_account",
				"fieldtype": "Link",
				"label": "Payment Account",
				"options": "Account",
				"insert_after": "payment_gateway",
			},
		],
		"Payment Request": [
			{
				"fieldname": "payment_details_section",
				"fieldtype": "Section Break",
				"label": "Payment Gateway Details",
				"depends_on": "eval: !doc.bank_account",
				"insert_after": "accounting_dimensions_section",
			},
			{
				"fieldname": "payment_gateway",
				"fieldtype": "Link",
				"label": "Payment Gateway",
				"options": "Payment Gateway",
				"insert_after": "payment_details_section",
			},
			{
				"fieldname": "payment_account",
				"fieldtype": "Link",
				"label": "Payment Account",
				"options": "Account",
				"mandatory_depends_on": "eval: doc.payment_gateway",
				"insert_after": "payment_gateway",
			},
			{
				"fieldname": "payment_channel",
				"fieldtype": "Data",
				"label": "Payment Channel",
				"read_only": 1,
				"insert_after": "payment_account",
			},
			{
				"fieldname": "column_break_pnyv",
				"fieldtype": "Column Break",
				"insert_after": "payment_channel",
			},
			{
				"fieldname": "payment_url",
				"fieldtype": "Data",
				"label": "Payment URL",
				"length": 500,
				"options": "URL",
				"read_only": 1,
				"insert_after": "column_break_pnyv",
			},
			{
				"fieldname": "phone_number",
				"fieldtype": "Data",
				"label": "Phone Number",
				"options": "Phone",
				"mandatory_depends_on": "eval: doc.payment_channel == 'Phone'",
				"insert_after": "payment_url",
			},
		],
	}


def delete_custom_fields():
	if not frappe.get_meta("Web Form").has_field("payments_tab"):
		return

	click.secho("* Uninstalling Payments Custom Fields from Web Form")
	frappe.db.delete(
		"Custom Field",
		{
			"dt": "Web Form",
			"fieldname": (
				"in",
				(
					"payments_tab",
					"accept_payment",
					"payment_gateway",
					"payment_button_label",
					"payment_button_help",
					"payments_cb",
					"amount_field",
					"amount_based_on_field",
					"amount",
					"currency",
				),
			),
		},
	)

	frappe.clear_cache(doctype="Web Form")


def delete_payments_erpnext_custom_fields():
	if "erpnext" not in frappe.get_installed_apps() or "payments" not in frappe.get_installed_apps():
		return

	custom_fields = {
		"GoCardless Mandate": ("customer",),
		"Payment Gateway Account": ("company", "payment_account"),
		"Payment Gateway": ("pga_section", "payment_gateway_account"),
		"Subscription Plan": ("payment_gateway", "payment_account"),
		"Payment Request": (
			"payment_details_section",
			"payment_gateway",
			"payment_account",
			"payment_channel",
			"column_break_pnyv",
			"payment_url",
			"phone_number",
		),
	}

	for doctype, fieldnames in custom_fields.items():
		if not frappe.get_meta(doctype):
			continue

		click.secho(f"* Uninstalling Payments Custom Fields from {doctype}")

		frappe.db.delete(
			"Custom Field",
			{
				"dt": doctype,
				"fieldname": ("in", fieldnames),
			},
		)

		frappe.clear_cache(doctype=doctype)


def before_install():
	# TODO: remove this
	# This is done for erpnext CI patch test
	#
	# Since we follow a flow like install v14 -> restore v10 site
	# -> migrate to v12, v13 and then v14 again
	#
	# This app fails installing when the site is restored to v10 as
	# a lot of apis don;t exist in v10 and this is a (at the moment) required app for erpnext.
	if not frappe.get_meta("Module Def").has_field("custom"):
		return False


@contextmanager
def erpnext_app_import_guard():
	marketplace_link = '<a href="https://frappecloud.com/marketplace/apps/erpnext">Marketplace</a>'
	github_link = '<a href="https://github.com/frappe/erpnext">GitHub</a>'
	msg = _("erpnext app is not installed. Please install it from {} or {}").format(
		marketplace_link, github_link
	)
	try:
		yield
	except ImportError:
		frappe.throw(msg, title=_("Missing ERPNext App"))


def validate_erpnext_compatibility():
	"""Ensure ERPNext app is compatible before site migration."""

	if "erpnext" not in frappe.get_installed_apps():
		return

	try:
		erpnext_utils = import_module("erpnext.setup.utils")
	except Exception:
		frappe.throw(_("Unable to load Erpnext utilities.\n\n") + frappe.get_traceback())

	if not hasattr(erpnext_utils, "validate_payments_compatibility"):
		frappe.throw(
			_("Incompatible ERPNext app version detected. Please update the ERPNext app before migration.")
		)
