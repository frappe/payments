from contextlib import contextmanager
from typing import TYPE_CHECKING

import click
import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.rate_limiter import rate_limit
from frappe.utils import flt

from payments.types import PaymentUrl, PSLName

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


def error_ref(error_log) -> str:
	"""Short, opaque correlation code for a guest-facing message.

	Takes the trailing 8 characters of the Error Log's *name*, so support can
	correlate against the server-side row without the guest seeing frappe's
	internal naming and timestamp. (M4)

	Deliberately not str(error_log): Document.__str__ is
	f"{doctype} ({name})", so that produced 7 characters of the name plus a
	stray ")" — a code that appears in no Error Log, which broke the very
	support path this replaced.
	"""
	return getattr(error_log, "name", str(error_log))[-8:]


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
	except frappe.DoesNotExistError:
		# The ordinary "no such gateway" answer, and the one the classification
		# tests exercise. Not worth a log.
		return False
	except Exception:
		# Anything else means we could not tell which generation this gateway is,
		# and the caller will treat it as v1 — so the real cause has to be
		# recorded somewhere, or a transient failure silently reclassifies a v2
		# gateway and the guest sees only "something is wrong with this site's
		# payment gateway configuration".
		frappe.log_error(title="Could not classify a payment gateway", message=frappe.get_traceback())
		return False

	# Defensive check: handle both class and instance (even though the function
	# currently always returns an instance, this ensures API robustness)
	if isinstance(controller, type):
		return issubclass(controller, PaymentController)
	return isinstance(controller, PaymentController)


def build_checkout_url(payment_gateway: str, **kwargs):
	"""Build a checkout URL for either gateway generation.

	NOT itself a whitelisted endpoint, and deliberately so: for a v2 gateway this
	creates a Payment Session Log from its arguments, and amount, currency and the
	reference document all come from the caller. A whitelisted, guest-callable
	version of this would let anyone open a session for an arbitrary amount
	against an arbitrary document. (Such rows sit at "Created", which retention
	now DOES purge via ABANDONED_STATES — so the argument is the fabricated money
	record itself, not that it is immortal. That sentence used to claim the
	latter, and the change adding ABANDONED_STATES falsified it in the same diff.)

	That is a statement about the endpoint, not an auth boundary: a guest DOES
	reach this function through payment_webform.accept, which is allow_guest by
	design. What constrains it there is the Web Form's own configuration —
	the operator sets the amount, the currency and the reference doctype — not
	anything this function checks. The guest-facing get_checkout_url below is the
	whitelisted surface, and it handles v1 only.

	A misconfigured gateway raises (get_payment_gateway_controller throws), which
	is what develop's Web Form path did too.
	"""
	if is_v2_gateway(payment_gateway):
		return _v2_checkout_url(payment_gateway, **kwargs)

	doc = get_payment_gateway_controller(payment_gateway)
	# Forward the gateway name explicitly. It is a named parameter here, so unlike
	# in **kwargs-passing callers it is NOT part of kwargs — and Stripe's checkout
	# page lists payment_gateway in expected_keys and redirect_to_message's
	# "someone sent you to an incomplete URL" without it. The Web Form's kwargs
	# have never carried it, on this branch or on develop.
	return doc.get_payment_url(payment_gateway=payment_gateway, **kwargs)


# Guest-callable and pre-existing on develop: the public v1 checkout entry point.
# Its exposure is held at exactly develop's — see the resolution note in the body
# — and it deliberately does NOT handle v2 gateways, since that would mean
# creating a Payment Session Log from guest-supplied amount and reference
# document. Server-side callers use build_checkout_url above.
#
# GET is still allowed because develop placed no method restriction here and this
# is a published endpoint of a distributed app: POST-only would 405 any existing
# caller, while buying nothing against an attacker who can simply POST.
# The suppression sits on THIS line, not on the closing paren: semgrep anchors the
# finding to the decorator's first line, and `ruff format` splits the call because
# it exceeds the line length, so a trailing comment lands two lines too low and
# stops suppressing. Verified against the CI rule set.
@frappe.whitelist(  # nosemgrep: guest-whitelisted-method
	allow_guest=True, xss_safe=True, methods=["GET", "POST"]
)
# Not keyed on any request field: frappe derives the bucket from ip:form_dict[key],
# so keying on payment_gateway would have handed the caller a fresh bucket per
# gateway name it cared to invent, each miss costing an Error Log insert below.
@rate_limit(limit=10, seconds=60)
def get_checkout_url(**kwargs):
	try:
		gateway = kwargs.get("payment_gateway")
		if not gateway:
			raise ValueError("no payment_gateway given")

		# The two generations name the same method with incompatible contracts: v1
		# is an instance method taking **kwargs, v2's is a staticmethod taking a
		# session name. Refuse v2 here rather than adapting, because adapting means
		# creating the money spine from guest input.
		if is_v2_gateway(gateway):
			raise ValueError(f"{gateway} is a v2 gateway; use build_checkout_url server-side")

		# Resolve the way develop does, NOT through get_payment_gateway_controller.
		# That helper honours gateway_settings/gateway_controller, so it resolves
		# the multi-instance gateways (Stripe-<name>, Braintree, GoCardless, Mpesa)
		# that develop's f-string could not reach from here — which would widen a
		# guest-callable money endpoint. build_checkout_url above has the general
		# resolution, for server-side callers.
		doc = frappe.get_doc(f"{gateway} Settings")
		return doc.get_payment_url(**kwargs)
	except Exception:
		frappe.log_error(title="Could not build a checkout URL", message=frappe.get_traceback())
		frappe.respond_as_web_page(
			_("Something went wrong"),
			_(
				"Looks like something is wrong with this site's payment gateway configuration. No payment has been made."
			),
			indicator_color="red",
			http_status_code=frappe.ValidationError.http_status_code,
		)


def _v2_checkout_url(gateway: str, **kwargs) -> PaymentUrl:
	"""Adapt the v1 `get_payment_url(**kwargs)` call shape onto the v2 contract.

	v2 splits what v1 did in one call: create a session from the transaction
	data, then hand back that session's URL.
	"""
	from payments.controllers import PaymentController
	from payments.types import TxData

	payer_contact = {}
	if kwargs.get("payer_name"):
		payer_contact["full_name"] = kwargs["payer_name"]
	if kwargs.get("payer_email"):
		payer_contact["email_id"] = kwargs["payer_email"]

	tx_data = TxData(
		amount=flt(kwargs.get("amount")),
		currency=kwargs.get("currency"),
		reference_doctype=kwargs.get("reference_doctype"),
		reference_docname=kwargs.get("reference_docname"),
		payer_contact=payer_contact,
		payer_address={},
		loyalty_points=None,
		discount_amount=None,
	)
	_controller, psl_name = PaymentController.initiate(tx_data, gateway)
	return PaymentController.get_payment_url(psl_name)


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
	make_payment_request_custom_fields()

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
		}

		create_custom_fields(custom_fields)


def make_payment_request_custom_fields():
	"""Create the Payment Request -> Payment Session Log reference.

	Deliberately not inside make_custom_fields' `if not
	has_field("payments_tab")` block. That guard is about the Web Form tab, and
	on any site that already had it — i.e. every existing install — the whole
	block was skipped, so this field was silently never created. Being its own
	idempotent function also lets a patch call it.

	Scope: nothing in the installed ERPNext v16.30.0 reads this field (grep finds
	zero occurrences of `payment_session_log` there). It is the correlation point
	this branch offers a reference document, and it is prospective until a
	consumer lands.
	"""
	# This app does not require ERPNext (see erpnext_app_import_guard, and
	# develop's own removal of erpnext from the bench dependencies), and
	# frappe.get_meta raises DoesNotExistError for an absent doctype — which,
	# because this now runs from a patch, would abort every bench migrate on such
	# a site.
	if not frappe.db.exists("DocType", "Payment Request"):
		return

	if frappe.get_meta("Payment Request").has_field("payment_session_log"):
		return

	click.secho("* Installing Payment Session Log reference on Payment Request")
	create_custom_fields(
		{
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
	)
	frappe.clear_cache(doctype="Payment Request")


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
