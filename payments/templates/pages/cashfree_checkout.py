# Copyright (c) 2025, Frappe Technologies and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, flt

from payments.utils.utils import validate_integration_request

no_cache = 1

expected_keys = (
    "amount",
    "title",
    "description",
    "reference_doctype",
    "reference_docname",
    "payer_name",
    "payer_email",
    "payer_phone",
    "order_id",
    "currency",
)


def get_context(context):
    context.no_cache = 1

    try:
        validate_integration_request(frappe.form_dict["token"])

        doc = frappe.get_doc("Integration Request", frappe.form_dict["token"])
        payment_details = json.loads(doc.data)

        for key in expected_keys:
            context[key] = payment_details.get(key, "")

        context["token"] = frappe.form_dict["token"]
        context["amount"] = flt(context["amount"])
        
        # Create Cashfree order
        cashfree_order = frappe.get_doc("Cashfree Settings").create_order(token=frappe.form_dict["token"])
        context["payment_session_id"] = cashfree_order.get("payment_session_id")
        context["use_sandbox"] = frappe.db.get_single_value("Cashfree Settings", "use_sandbox")

    except Exception as e:
        frappe.redirect_to_message(
            _("Invalid Token"),
            _("Seems token you are using is invalid!"),
            http_status_code=400,
            indicator_color="red",
        )

        frappe.local.flags.redirect_location = frappe.local.response.location
        raise frappe.Redirect

