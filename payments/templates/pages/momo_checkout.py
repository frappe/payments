import frappe, json
from frappe import _

def get_context(context):
    token = frappe.form_dict.get("token")
    if not token:
        frappe.throw(_("Invalid or missing payment token"), frappe.PermissionError)

    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        frappe.throw(_("Payment session expired. Please go back to your cart and try again."))

    cart_data = json.loads(raw)

    context.update({
        "token": token,
        "amount": frappe.form_dict.get("amount") or cart_data["grand_total"],
        "currency": cart_data["currency"],
        "gateway_name": frappe.form_dict.get("gateway_name"),
        "title": frappe.form_dict.get("title") or "Complete Your Payment",
        "customer_name": cart_data.get("customer_name", ""),
        "no_cache": 1,
    })
