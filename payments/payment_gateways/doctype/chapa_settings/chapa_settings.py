# Copyright (c) 2026, TechVision and contributors
# License: MIT

import uuid
import re
from decimal import Decimal, InvalidOperation

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
        # Chapa Settings is a Single DocType, so it cannot be the target of the
        # Payment Gateway's Dynamic Link field. With no controller stored on the
        # gateway, get_payment_gateway_controller() loads "Chapa Settings"
        # directly, as it does for the other single-settings gateways.
        create_payment_gateway("Chapa")

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

        tx_ref = f"{self.data.payment}-{uuid.uuid4().hex[:10]}"

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
                "/api/method/payments.payment_gateways.doctype."
                "chapa_settings.chapa_settings.verify_payment"
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
    # VERIFY PAYMENT + AUTO ENROLLMENT
    # -----------------------------


@frappe.whitelist(allow_guest=True)
def verify_payment():
    tx_ref = (
        frappe.form_dict.get("trx_ref")
        or frappe.form_dict.get("tx_ref")
    )

    if not tx_ref:
        frappe.throw(_("Missing tx_ref"))

    settings = frappe.get_doc("Chapa Settings", "Chapa Settings")
    
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
    payment_data = result.get("data") or {}

    if (
        result.get("status") != "success"
        or payment_data.get("status") != "success"
    ):
        frappe.throw(_("Payment verification failed"))

    if payment_data.get("tx_ref") != tx_ref:
        frappe.throw(_("Payment reference mismatch"))

    # Extract LMS Payment document name from:
    # LMS-PAY-00001-<random Chapa suffix>
    payment_name = tx_ref.rsplit("-", 1)[0]

    if not frappe.db.exists("LMS Payment", payment_name):
        frappe.throw(_("LMS Payment not found"))

    payment = frappe.get_doc("LMS Payment", payment_name)

    try:
        paid_amount = Decimal(str(payment_data.get("amount")))
        expected_amount = Decimal(str(payment.amount_with_gst or payment.amount))
    except (InvalidOperation, TypeError, ValueError):
        frappe.throw(_("Invalid payment amount returned by Chapa"))

    if paid_amount != expected_amount or payment_data.get("currency") != payment.currency:
        frappe.throw(_("Payment amount or currency mismatch"))

    if not payment.member:
        frappe.throw(_("Payment member not found"))

    if not payment.payment_received:
        from lms.lms.utils import complete_enrollment

        payment.payment_received = 1
        payment.payment_id = payment_data.get("reference")
        payment.save(ignore_permissions=True)

        # Chapa calls this endpoint as Guest. LMS enrollment uses
        # frappe.session.user, so switch temporarily to the actual payer.
        original_user = frappe.session.user

        try:
            frappe.set_user(payment.member)

            # Automatically creates LMS Enrollment, LMS Batch Enrollment,
            # or certificate-purchase state based on the LMS Payment record.
            complete_enrollment(
                payment.name,
                payment.payment_for_document_type,
                payment.payment_for_document,
            )

        finally:
            frappe.set_user(original_user)

        frappe.db.commit()

    return {
        "status": "success",
        "tx_ref": tx_ref,
        "data": payment_data,
    }