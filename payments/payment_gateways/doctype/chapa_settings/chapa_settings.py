# Copyright (c) 2026, TechVision and contributors
# License: MIT

from urllib.parse import urlencode

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from payments.utils import create_payment_gateway


class ChapaSettings(Document):
    supported_currencies = ("ETB",)

    def on_update(self):
        create_payment_gateway(
            "Chapa",
            settings="Chapa Settings",
            controller="Chapa",
        )

        if not self.flags.ignore_mandatory:
            self.validate_credentials()

    def validate_credentials(self):
        """Validate Secret Key by calling Chapa banks endpoint."""

        if not self.secret_key:
            return

        headers = {
            "Authorization": f"Bearer {self.get_password('secret_key')}"
        }

        try:
            response = requests.get(
                "https://api.chapa.co/v1/banks",
                headers=headers,
                timeout=15,
            )

            if response.status_code != 200:
                frappe.throw(_("Invalid Chapa Secret Key"))

        except requests.RequestException:
            frappe.throw(_("Unable to connect to Chapa."))

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _("Chapa only supports transactions in ETB.")
            )

    def get_payment_url(self, **kwargs):
        return get_url(f"./chapa_checkout?{urlencode(kwargs)}")

    def create_request(self, data):
        """
        Called by Payment Request.
        """

        self.data = frappe._dict(data)

        return self.initialize_transaction()

    def initialize_transaction(self):
        headers = {
            "Authorization": f"Bearer {self.get_password('secret_key')}",
            "Content-Type": "application/json",
        }

        payload = {
            "amount": str(self.data.amount),
            "currency": self.data.currency,
            "email": self.data.payer_email,
            "first_name": self.data.payer_name,
            "tx_ref": self.data.reference_docname,
            "callback_url": get_url(
                "/api/method/payments.payment_gateways.doctype.chapa_settings.chapa_settings.chapa_callback"
            ),
            "return_url": get_url(
                "/payment-success"
            ),
            "customization": {
                "title": frappe.db.get_single_value(
                    "Website Settings",
                    "app_name"
                ),
                "description": self.data.description,
            },
        }

        try:
            response = requests.post(
                "https://api.chapa.co/v1/transaction/initialize",
                json=payload,
                headers=headers,
                timeout=30,
            )

            response.raise_for_status()

            result = response.json()

            if result.get("status") != "success":
                frappe.throw(result.get("message"))

            return {
                "redirect_to": result["data"]["checkout_url"],
                "status": "Initialized",
            }

        except Exception:
            frappe.log_error(frappe.get_traceback(), "Chapa Initialize")
            frappe.throw(_("Unable to initialize Chapa payment."))