# Copyright (c) 2026, TechVision and contributors
# License: MIT

import re
import uuid

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from payments.utils import create_payment_gateway


class ChapaSettings(Document):
    supported_currencies = ("ETB",)

    # ---------------------------------------------------------
    # REGISTER PAYMENT GATEWAY
    # ---------------------------------------------------------

    def on_update(self):
        create_payment_gateway(
            "Chapa",
            settings=self.name,
            controller="ChapaSettings",
        )

        self.validate_credentials()

    # ---------------------------------------------------------
    # VALIDATE CHAPA CREDENTIALS
    # ---------------------------------------------------------

    def validate_credentials(self):
        if not self.secret_key:
            return

        headers = {
            "Authorization": (
                f"Bearer {self.get_password('secret_key')}"
            )
        }

        try:
            response = requests.get(
                "https://api.chapa.co/v1/banks",
                headers=headers,
                timeout=10,
            )

            if response.status_code != 200:
                frappe.throw(
                    _("Invalid Chapa Secret Key")
                )

        except requests.RequestException:
            frappe.throw(
                _("Unable to connect to Chapa API")
            )

    # ---------------------------------------------------------
    # CURRENCY
    # ---------------------------------------------------------

    def validate_transaction_currency(self, currency):
        if currency and currency not in self.supported_currencies:
            frappe.throw(
                _("Chapa only supports ETB transactions")
            )

    # ---------------------------------------------------------
    # PAYMENT URL
    # ---------------------------------------------------------

    def get_payment_url(self, **kwargs):
        self.data = frappe._dict(kwargs)

        self.validate_transaction_currency(
            self.data.get("currency")
        )

        return self.initialize_transaction()

    def create_request(self, data):
        return self.get_payment_url(**data)

    # ---------------------------------------------------------
    # INITIALIZE CHAPA PAYMENT
    # ---------------------------------------------------------

    def initialize_transaction(self):
        url = (
            "https://api.chapa.co/v1/"
            "transaction/initialize"
        )

        headers = {
            "Authorization": (
                f"Bearer {self.get_password('secret_key')}"
            ),
            "Content-Type": "application/json",
        }

        # IMPORTANT:
        # LMS Payment name is the reference document.
        #
        # Example:
        #
        # LMS Payment = learn-html
        #
        # tx_ref =
        # learn-html-ca4c80f7bc

        payment_name = self.data.get("payment")

        if not payment_name:
            payment_name = self.data.get(
                "reference_docname"
            )

        if not payment_name:
            frappe.throw(
                _("Payment reference is missing")
            )

        tx_ref = (
            f"{payment_name}-"
            f"{uuid.uuid4().hex[:10]}"
        )

        # -----------------------------------------------------
        # CUSTOMER EMAIL
        # -----------------------------------------------------

        email = (
            self.data.get("payer_email")
            or frappe.session.user
        )

        if (
            email == "Guest"
            or "@" not in email
        ):
            email = "mehariwamlake@gmail.com"

        # -----------------------------------------------------
        # CUSTOMER NAME
        # -----------------------------------------------------

        payer_name = (
            self.data.get("payer_name")
            or "Customer"
        )

        payer_name = str(payer_name)[:30]

        # -----------------------------------------------------
        # TITLE
        # -----------------------------------------------------

        title = (
            self.data.get("title")
            or "Payment for LMS"
        )

        title = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            str(title),
        )

        title = title[:16]

        # -----------------------------------------------------
        # DESCRIPTION
        # -----------------------------------------------------

        description = (
            self.data.get("description")
            or "LMS Payment"
        )

        description = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            str(description),
        )

        description = description[:49]

        # -----------------------------------------------------
        # CHAPA PAYLOAD
        # -----------------------------------------------------

        payload = {
            "amount": str(
                self.data.get("amount")
            ),
            "currency": "ETB",

            "email": email,

            "first_name": payer_name,

            "last_name": "",

            "phone_number": (
                self.data.get("phone_number")
                or ""
            ),

            "tx_ref": tx_ref,

            # Chapa will call this after payment.
            "callback_url": get_url(
                "/api/method/"
                "payments.payment_gateways.doctype."
                "chapa_settings.chapa_settings."
                "verify_payment"
            ),

            # User is redirected here after payment.
            "return_url": get_url(
                self.data.get("redirect_to")
                or "/"
            ),

            "customization": {
                "title": title,
                "description": description,
            },
        }

        # -----------------------------------------------------
        # SEND REQUEST TO CHAPA
        # -----------------------------------------------------

        try:
            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=30,
            )

            frappe.logger().info(
                "CHAPA INITIALIZE STATUS: "
                f"{response.status_code}"
            )

            frappe.logger().info(
                "CHAPA INITIALIZE RESPONSE: "
                f"{response.text}"
            )

            response.raise_for_status()

            result = response.json()

            if result.get("status") != "success":
                frappe.throw(
                    str(result)
                )

            checkout_url = (
                result
                .get("data", {})
                .get("checkout_url")
            )

            if not checkout_url:
                frappe.throw(
                    _("Chapa did not return a checkout URL")
                )

            return checkout_url

        except requests.HTTPError:
            frappe.throw(
                response.text
            )

        except requests.RequestException as e:
            frappe.throw(
                _("Unable to connect to Chapa: {0}").format(
                    str(e)
                )
            )

    # ---------------------------------------------------------
    # VERIFY PAYMENT
    # ---------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def verify_payment():
    """
    Chapa callback.

    Flow:

        Chapa
          ↓
        verify_payment()
          ↓
        verify transaction with Chapa
          ↓
        find LMS Payment
          ↓
        mark payment_received = 1
          ↓
        complete_enrollment()
          ↓
        LMS Enrollment
    """

    frappe.logger().info(
        "========== CHAPA VERIFY START =========="
    )

    # ---------------------------------------------------------
    # GET TX REF
    # ---------------------------------------------------------

    tx_ref = (
        frappe.form_dict.get("tx_ref")
        or frappe.request.args.get("tx_ref")
    )

    frappe.logger().info(
        f"CHAPA TX_REF: {tx_ref}"
    )

    if not tx_ref:
        frappe.throw(
            _("Missing Chapa transaction reference")
        )

    # ---------------------------------------------------------
    # GET CHAPA SETTINGS
    # ---------------------------------------------------------

    settings_name = frappe.db.get_value(
        "Chapa Settings",
        {"gateway_name": "Chapa"},
        "name",
    )

    if not settings_name:

        # For Single DocType installations,
        # this is normally the document name.
        if frappe.db.exists(
            "Chapa Settings",
            "Chapa Settings",
        ):
            settings_name = "Chapa Settings"

    if not settings_name:
        frappe.throw(
            _("Chapa Settings configuration was not found")
        )

    settings = frappe.get_doc(
        "Chapa Settings",
        settings_name,
    )

    # ---------------------------------------------------------
    # VERIFY WITH CHAPA
    # ---------------------------------------------------------

    headers = {
        "Authorization": (
            f"Bearer "
            f"{settings.get_password('secret_key')}"
        )
    }

    verify_url = (
        "https://api.chapa.co/v1/"
        "transaction/verify/"
        f"{tx_ref}"
    )

    try:
        response = requests.get(
            verify_url,
            headers=headers,
            timeout=15,
        )

        response.raise_for_status()

    except requests.RequestException as e:

        frappe.log_error(
            title="Chapa Verification Error",
            message=frappe.get_traceback(),
        )

        frappe.throw(
            _("Unable to verify Chapa payment: {0}").format(
                str(e)
            )
        )

    result = response.json()

    frappe.logger().info(
        "CHAPA VERIFY RESPONSE: "
        f"{result}"
    )

    # ---------------------------------------------------------
    # CHAPA RESPONSE STATUS
    # ---------------------------------------------------------

    if result.get("status") != "success":

        frappe.throw(
            _("Chapa payment verification failed")
        )

    payment_data = result.get(
        "data",
        {},
    )

    # ---------------------------------------------------------
    # ACTUAL TRANSACTION STATUS
    # ---------------------------------------------------------

    if payment_data.get("status") != "success":

        frappe.throw(
            _("Chapa payment was not successful")
        )

    # ---------------------------------------------------------
    # GET LMS PAYMENT
    # ---------------------------------------------------------

    # We generated:
    #
    # learn-html-ca4c80f7bc
    #
    # Remove only the random suffix.

    payment_name = tx_ref.rsplit(
        "-",
        1,
    )[0]

    frappe.logger().info(
        f"LOOKING FOR LMS PAYMENT: {payment_name}"
    )

    # ---------------------------------------------------------
    # FIND LMS PAYMENT
    # ---------------------------------------------------------

    if not frappe.db.exists(
        "LMS Payment",
        payment_name,
    ):

        # -----------------------------------------------------
        # FALLBACK:
        # Search by Order ID / payment_id / reference
        # -----------------------------------------------------

        payment_name = frappe.db.get_value(
            "LMS Payment",
            {
                "order_id": tx_ref,
            },
            "name",
        )

    if not payment_name:

        frappe.log_error(
            title="Chapa LMS Payment Not Found",
            message=(
                f"Transaction: {tx_ref}\n"
                f"Chapa Response:\n"
                f"{frappe.as_json(payment_data)}"
            ),
        )

        frappe.throw(
            _(
                "Payment was verified successfully, "
                "but the LMS Payment could not be found. "
                "Transaction: {0}"
            ).format(tx_ref)
        )

    # ---------------------------------------------------------
    # LOAD LMS PAYMENT
    # ---------------------------------------------------------

    payment = frappe.get_doc(
        "LMS Payment",
        payment_name,
    )

    frappe.logger().info(
        "LMS PAYMENT FOUND: "
        f"{payment.name}"
    )

    # ---------------------------------------------------------
    # MARK PAYMENT AS RECEIVED
    # ---------------------------------------------------------

    if not payment.payment_received:

        payment.payment_received = 1

        # Chapa's unique payment reference
        payment.payment_id = str(
            payment_data.get("reference")
            or payment_data.get("id")
            or tx_ref
        )

        # Store tx_ref in order_id if available.
        if payment.meta.has_field(
            "order_id"
        ):
            payment.order_id = tx_ref

        payment.save(
            ignore_permissions=True
        )

        frappe.db.commit()

        frappe.logger().info(
            "LMS PAYMENT MARKED AS RECEIVED: "
            f"{payment.name}"
        )

    # ---------------------------------------------------------
    # COMPLETE LMS ENROLLMENT
    # ---------------------------------------------------------

    try:

        from lms.lms.utils import (
            complete_enrollment,
        )

        payment_for_document_type = (
            payment.get(
                "payment_for_document_type"
            )
        )

        payment_for_document = (
            payment.get(
                "payment_for_document"
            )
        )

        frappe.logger().info(
            "COMPLETING LMS ENROLLMENT: "
            f"type={payment_for_document_type}, "
            f"document={payment_for_document}, "
            f"payment={payment.name}"
        )

        if (
            payment_for_document_type
            and payment_for_document
        ):

            complete_enrollment(
                payment.name,
                payment_for_document_type,
                payment_for_document,
            )

            frappe.db.commit()

            frappe.logger().info(
                "LMS ENROLLMENT COMPLETED: "
                f"{payment.name}"
            )

        else:

            frappe.log_error(
                title="Chapa Enrollment Data Missing",
                message=(
                    f"LMS Payment: {payment.name}\n"
                    f"Payment For Document Type: "
                    f"{payment_for_document_type}\n"
                    f"Payment For Document: "
                    f"{payment_for_document}"
                ),
            )

    except Exception:

        frappe.log_error(
            title="Chapa LMS Enrollment Error",
            message=frappe.get_traceback(),
        )

        raise

    # ---------------------------------------------------------
    # FINAL RESPONSE
    # ---------------------------------------------------------

    frappe.logger().info(
        "========== CHAPA VERIFY SUCCESS =========="
    )

    return {
        "status": "success",
        "tx_ref": tx_ref,
        "payment": payment.name,
        "payment_received": payment.payment_received,
        "enrollment": True,
        "data": payment_data,
    }