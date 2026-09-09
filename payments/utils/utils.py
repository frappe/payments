from contextlib import contextmanager
from typing import TYPE_CHECKING

import click
import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from payments.types import PSLName

if TYPE_CHECKING:
	from frappe.model.document import Document

	from payments.controllers import PaymentController
	from payments.payments.doctype.payment_session_log.payment_session_log import (
		PaymentSessionLog,
	)

# Key used to identify the payment session on the frappe/erpnext side across its lifecycle
PAYMENT_SESSION_REF_KEY = "s"


def validate_integration_request(docname: str | None):
	if frappe.db.get_value("Integration Request", docname, "status") == "Cancelled":
		frappe.throw(_("Expired Token"))


def get_payment_gateway_controller(payment_gateway: str) -> "Document":
	"""Return the payment gateway controller settings document instance.

	This function always returns a Document **instance** (never a class).

	The returned instance is the settings document for the gateway (e.g., "Stripe Settings"),
	which may or may not inherit from PaymentController:
	- V2 gateways: Instance of a Document subclass that inherits from PaymentController
	- V1 gateways: Instance of a Document subclass that does NOT inherit from PaymentController

	Use `is_v2_gateway()` to check if a gateway implements the PaymentController interface.

	Args:
	        payment_gateway: The name of the Payment Gateway document

	Returns:
	        Document instance of the gateway's settings DocType

	Raises:
	        frappe.ValidationError: If the gateway settings document is not found
	"""
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


def is_v2_gateway(payment_gateway: str) -> bool:
	"""Check if a payment gateway implements the PaymentController interface (v2).

	This function safely determines whether a gateway uses the new PaymentController
	architecture (v2) or the legacy architecture (v1). Use this to conditionally
	choose the appropriate payment flow.

	The check is defensive and handles both edge cases:
	- If controller is a class: uses issubclass()
	- If controller is an instance: uses isinstance()

	Note: get_payment_gateway_controller() always returns an instance, but this
	defensive check is maintained for API robustness.

	Args:
	        payment_gateway: The name of the Payment Gateway document

	Returns:
	        True if the gateway implements PaymentController (v2), False otherwise.
	        Returns False if the gateway doesn't exist or PaymentController cannot be imported.
	"""
	if not payment_gateway:
		return False

	try:
		from payments.controllers import PaymentController
	except ImportError:
		return False

	try:
		controller = get_payment_gateway_controller(payment_gateway)
	except Exception:
		return False

	# Defensive check: handle both class and instance (even though the function
	# currently always returns an instance, this ensures API robustness)
	if isinstance(controller, type):
		return issubclass(controller, PaymentController)
	return isinstance(controller, PaymentController)


@frappe.whitelist(allow_guest=True, xss_safe=True)
def get_checkout_url(**kwargs):
	try:
		if kwargs.get("payment_gateway"):
			doc = get_payment_gateway_controller(kwargs.get("payment_gateway"))
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
				"gateway": gateway,
				"gateway_settings": settings,
				"gateway_controller": controller,
			}
		)
		payment_gateway.insert(ignore_permissions=True)


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

	if "erpnext" in frappe.get_installed_apps():
		custom_fields = {
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
			"Payment Request": [
				{
					"fieldname": "payment_session_log",
					"fieldtype": "Link",
					"in_list_view": 1,
					"label": "Payment Session Log",
					"options": "Payment Session Log",
					"read_only": 1,
					"insert_after": "payment_url",
				}
			],
		}

		create_custom_fields(custom_fields)


def delete_custom_fields():
	if not frappe.get_meta("Web Form").has_field("payments_tab"):
		return

	click.secho("* Uninstalling Payment Custom Fields from Web Form")

	fieldnames = (
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
	)

	for fieldname in fieldnames:
		frappe.db.delete("Custom Field", {"name": "Web Form-" + fieldname})

	frappe.db.delete("Custom Field", {"name": "Payment Request-payment_session_log"})

	frappe.clear_cache(doctype="Web Form")


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
