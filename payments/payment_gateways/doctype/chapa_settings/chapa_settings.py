# Copyright (c) 2026, TechVision and contributors
# License: MIT

import uuid
import re
import requests
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from payments.utils import create_payment_gateway


class ChapaSettings(Document):
    
    supported_currencies = ("ETB",)
    

    # -----------------------------
    # REGISTER GATEWAY
    # -----------------------------
    def on_update(self):
        create_payment_gateway(
            "Chapa",
            settings="Chapa Settings",
            controller="ChapaSettings",
        )

        self.validate_credentials()

    # -----------------------------
    # VALIDATE SECRET KEY
    # -----------------------------
    def validate_credentials(self):
        if not self.secret_key:
            return

        headers = {
            "Authorization": f"Bearer {self.get_password('secret_key')}"
        }

        try:
            r = requests.get(
                "https://api.chapa.co/v1/banks",
                headers=headers,
                timeout=10,
            )

            if r.status_code != 200:
                frappe.throw(_("Invalid Chapa Secret Key"))

        except requests.RequestException:
            frappe.throw(_("Unable to connect to Chapa API"))

    supported_currencies = ("ETB",)

    def validate_transaction_currency(self, currency):
        if currency and currency not in self.supported_currencies:
            frappe.throw(_("Chapa only supports ETB transactions"))
    # -----------------------------
    # CALLED BY LMS / PAYMENT FLOW
    # -----------------------------
    def get_payment_url(self, **kwargs):
        self.validate_transaction_currency(kwargs.get("currency"))

        self.data = frappe._dict(kwargs)

        return self.initialize_transaction()
    
    def create_request(self, data):
        return self.get_payment_url(**data)
    # -----------------------------
    # INIT PAYMENT (CHAPA API)
    # -----------------------------
    def initialize_transaction(self):
        url = "https://api.chapa.co/v1/transaction/initialize"

        headers = {
            "Authorization": f"Bearer {self.get_password('secret_key')}",
            "Content-Type": "application/json",
        }

        tx_ref = f"{self.data.reference_docname}-{uuid.uuid4().hex[:10]}"

                

        email = (
            self.data.get("payer_email")
            or frappe.session.user
        )

        if email == "Guest" or "@" not in email:
            email = "mehariwamlake@gmail.com"

        title = self.data.get("title") or "Payment"
        title = re.sub(r"[^A-Za-z0-9_. -]", "", title)
        title = title[:16]

        description = self.data.get("description") or "i love you to pay"
        description = re.sub(r"[^A-Za-z0-9_. -]", "", description)
        description = description[:49]

        payload = {
            "amount": str(self.data.amount),
            "currency": "ETB",

            "email": email,

            "first_name": (
                self.data.get("payer_name")
                or "Customer"
            )[:30],

            "last_name": "",

            "phone_number": self.data.get("phone_number") or "",

            "tx_ref": tx_ref,

            "callback_url": get_url(
                "/api/method/payments.payment_gateways.doctype.chapa_settings.chapa_settings.verify_payment"
            ),

            "return_url": get_url(self.data.redirect_to or "/"),

            "customization": {
                "title": title,
                "description": description,
            },
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=30,
            )

            print("STATUS:", response.status_code)
            print("BODY:", response.text)

            response.raise_for_status()

            result = response.json()

            if result.get("status") != "success":
                frappe.throw(str(result))

            return result["data"]["checkout_url"]

        except requests.HTTPError:
            frappe.throw(response.text)

        except Exception:
            frappe.throw(frappe.get_traceback())

    # -----------------------------
    # VERIFY PAYMENT (WEBHOOK)
    # -----------------------------
@frappe.whitelist(allow_guest=True)
def verify_payment():
    tx_ref = frappe.form_dict.get("tx_ref")

    if not tx_ref:
        frappe.throw(_("Missing tx_ref"))

    settings = frappe.get_doc("Chapa Settings", "Chapa")

    headers = {
        "Authorization": f"Bearer {settings.get_password('secret_key')}"
    }

    r = requests.get(
        f"https://api.chapa.co/v1/transaction/verify/{tx_ref}",
        headers=headers,
        timeout=15,
    )

    r.raise_for_status()
    result = r.json()

    if result.get("status") != "success":
        frappe.throw(_("Payment verification failed"))

    payment_data = result.get("data", {})

    # Extract LMS Payment document name
    payment_name = tx_ref.rsplit("-", 1)[0]

    if frappe.db.exists("LMS Payment", payment_name):
    payment = frappe.get_doc("LMS Payment", payment_name)

    if not payment.payment_received:
        payment.payment_received = 1
        payment.payment_id = payment_data.get("id")

        # Save payment
        payment.save(ignore_permissions=True)

        # Trigger LMS enrollment
        if hasattr(payment, "on_payment_authorized"):
            payment.on_payment_authorized()

        if hasattr(payment, "create_enrollment"):
            payment.create_enrollment()

        frappe.db.commit()

    return {
        "status": "success",
        "tx_ref": tx_ref,
        "data": payment_data,
    }