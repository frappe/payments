import frappe
import requests
import uuid

from frappe.utils import get_url


CHAPA_URL = "https://api.chapa.co/v1/transaction/initialize"


@frappe.whitelist(allow_guest=True)
def chapa_checkout(
    amount,
    title,
    description,
    reference_doctype=None,
    reference_docname=None,
    payer_email=None,
    payer_name=None,
    currency="ETB",
    redirect_to="/",
):
    """
    Create Chapa payment session and return checkout URL
    """

    settings = frappe.get_single("Chapa Settings")

    secret_key = settings.get_password("secret_key")

    if not secret_key:
        frappe.throw("Chapa Secret Key is missing")

    # Unique transaction reference
    tx_ref = f"{reference_docname or 'LMS'}-{uuid.uuid4().hex[:10]}"

    payload = {
        "amount": str(amount),
        "currency": currency,

        "email": payer_email or frappe.session.user,

        "first_name": (
            payer_name or "Guest"
        )[:30],

        "last_name": "User",

        "tx_ref": tx_ref,

        "callback_url": get_url(
            "/api/method/payments.payment_gateways.doctype.chapa_settings.chapa_settings.verify_payment"
        ),

        "return_url": get_url(redirect_to),

        "customization": {
            "title": title[:16],
            "description": description[:49],
        },
    }

    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(
            CHAPA_URL,
            json=payload,
            headers=headers,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

    except requests.RequestException:
        frappe.throw(
            f"Chapa connection failed: {frappe.get_traceback()}"
        )

    if data.get("status") != "success":
        frappe.throw(
            f"Chapa Error: {data}"
        )

    checkout_url = data["data"]["checkout_url"]

    return {
        "checkout_url": checkout_url,
        "tx_ref": tx_ref,
    }