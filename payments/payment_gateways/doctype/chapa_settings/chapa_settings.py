# Copyright (c) 2026, TechVision and contributors
# License: MIT

import uuid
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

    # -----------------------------
    # CALLED BY LMS / PAYMENT FLOW
    # -----------------------------
    def get_payment_url(self, **kwargs):
        self.validate_transaction_currency(kwargs.get("currency"))

        self.data = frappe._dict(kwargs)

        return self.initialize_transaction()

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

        payload = {
            "amount": str(self.data.amount),
            "currency": "ETB",
            "email": self.data.payer_email or "mehariwamlake@gmail.com",
            "first_name": self.data.payer_name or "User",
            "last_name": "",
            "phone_number": self.data.get("phone_number", ""),

            "tx_ref": tx_ref,

            "callback_url": get_url(
                "/api/method/payments.payment_gateways.doctype.chapa_settings.chapa_settings.verify_payment"
            ),

            "return_url": get_url(self.data.redirect_to or "/"),

            "customization": {
                "title": self.data.title or "LMS Payment",
                "description": self.data.description or "",
            },
        }

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=30,
            )

            # IMPORTANT DEBUG
            if response.status_code != 200:
                frappe.log_error(response.text, "Chapa API Error")
                frappe.throw(_("Chapa API request failed"))

            result = response.json()
            print(result)

            if result.get("status") != "success":
                frappe.log_error(str(result), "Chapa Init Error")
                frappe.throw(_("Chapa rejected the request"))

            # 🔥 CRITICAL: redirect LMS directly
            frappe.local.response["type"] = "redirect"
            frappe.local.response["location"] = result["data"]["checkout_url"]

            return

        except Exception:
            frappe.log_error(frappe.get_traceback(), "Chapa Initialize Error")
            frappe.throw(_("Unable to initialize Chapa payment"))

    # -----------------------------
    # VERIFY PAYMENT (WEBHOOK)
    # -----------------------------
    @frappe.whitelist(allow_guest=True)
    def verify_payment(self):
        tx_ref = frappe.form_dict.get("tx_ref")

        if not tx_ref:
            frappe.throw(_("Missing tx_ref"))

        headers = {
            "Authorization": f"Bearer {self.get_password('secret_key')}"
        }

        r = requests.get(
            f"https://api.chapa.co/v1/transaction/verify/{tx_ref}",
            headers=headers,
            timeout=15,
        )

        r.raise_for_status()
        data = r.json()

        if data.get("status") != "success":
            frappe.throw(_("Payment verification failed"))

        return {
            "status": "success",
            "tx_ref": tx_ref,
            "data": data.get("data"),
        }

    # -----------------------------
    # VALIDATION
    # -----------------------------
    def validate_transaction_currency(self, currency):
        if currency and currency not in self.supported_currencies:
            frappe.throw(_("Chapa only supports ETB transactions"))