"""
MoMo checkout page — context builder.
Mirrors payments/templates/pages/gocardless_checkout.py.
"""

import frappe
from frappe import _
from frappe.utils import flt
from payments.utils import get_payment_gateway_controller

no_cache = 1

EXPECTED_KEYS = (
    "amount",
    "title",
    "description",
    "reference_doctype",
    "reference_docname",
    "payer_name",
    "payer_email",
    "order_id",
    "currency",
    "payment_request_name",
)


def get_context(context):
    context.no_cache = 1

    missing = set(EXPECTED_KEYS) - set(frappe.form_dict.keys())

    if missing:
        frappe.redirect_to_message(
            _("Missing parameters"),
            _("The payment link is incomplete. Missing: {0}").format(
                ", ".join(missing)
            ),
        )
        raise frappe.Redirect

    for key in EXPECTED_KEYS:
        context[key] = frappe.form_dict.get(key, "")

    context["amount"] = flt(context["amount"])

    # Resolve the gateway controller (MoMo Settings document)
    try:
        gateway_controller = get_payment_gateway_controller(
            frappe.form_dict.get("payment_gateway", "")
        )
        context["gateway_name"] = gateway_controller.gateway_name

    except Exception:
        context["gateway_name"] = ""

    # Page metadata
    context["title"] = context.get("title") or _("MTN MoMo Payment")
    context["card_title"] = _("Pay with MTN MoMo")
    context["frappe_csrf_token"] = frappe.session.csrf_token
