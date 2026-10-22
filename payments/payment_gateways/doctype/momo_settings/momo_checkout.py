import frappe
from frappe import _

def get_context(context):
    form_dict = frappe.form_dict

    pr_name = form_dict.get("payment_request_name") or form_dict.get("order_id")
    if not pr_name:
        frappe.throw(_("Invalid payment link"))

    pr = frappe.get_doc("Payment Request", pr_name)

    # Resolve gateway name (strip "MoMo-" prefix)
    gateway_full = pr.payment_gateway or ""
    gateway_name = gateway_full.replace("MoMo-", "") if gateway_full.startswith("MoMo-") else gateway_full

    context.update({
        "title": form_dict.get("title") or f"Payment for {pr.reference_name}",
        "amount": pr.grand_total,
        "currency": pr.currency,
        "order_id": pr_name,
        "payment_request_name": pr_name,
        "gateway_name": gateway_name,
        "reference_doctype": "Payment Request",
        "reference_docname": pr_name,
        "payer_name": pr.party_name,
        "success_url": f"/orders?name={pr.reference_name}",
    })
