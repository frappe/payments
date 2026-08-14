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

    # =========================================================
    # REGISTER PAYMENT GATEWAY
    # =========================================================

    def on_update(self):
        create_payment_gateway(
            "Chapa",
            settings=self.name,
            controller="ChapaSettings",
        )

        self.validate_credentials()

    # =========================================================
    # VALIDATE CHAPA CREDENTIALS
    # =========================================================

    def validate_credentials(self):

        if not self.secret_key:
            return

        headers = {
            "Authorization": f"Bearer {self.get_password('secret_key')}"
        }

        try:
            response = requests.get(
                "https://api.chapa.co/v1/banks",
                headers=headers,
                timeout=10,
            )

            if response.status_code != 200:
                frappe.throw(_("Invalid Chapa Secret Key"))

        except requests.RequestException:
            frappe.throw(
                _("Unable to connect to Chapa API")
            )

    # =========================================================
    # CURRENCY VALIDATION
    # =========================================================

    def validate_transaction_currency(self, currency):

        if currency and currency not in self.supported_currencies:
            frappe.throw(
                _("Chapa only supports ETB transactions")
            )

    # =========================================================
    # GET PAYMENT URL
    # =========================================================

    def get_payment_url(self, **kwargs):

        self.data = frappe._dict(kwargs)

        self.validate_transaction_currency(
            self.data.get("currency")
        )

        return self.initialize_transaction()

    # =========================================================
    # CREATE REQUEST
    # =========================================================

    def create_request(self, data):

        return self.get_payment_url(**data)

    # =========================================================
    # INITIALIZE CHAPA PAYMENT
    # =========================================================

    def initialize_transaction(self):

        url = "https://api.chapa.co/v1/transaction/initialize"

        secret_key = self.get_password("secret_key")

        if not secret_key:
            frappe.throw(
                _("Chapa Secret Key is not configured")
            )

        reference_docname = self.data.get(
            "reference_docname"
        )

        if not reference_docname:
            frappe.throw(
                _("Payment reference is missing")
            )

        # Unique Chapa transaction reference
        tx_ref = (
            f"{reference_docname}-"
            f"{uuid.uuid4().hex[:10]}"
        )

        frappe.logger().info(
            f"CHAPA INITIALIZE | "
            f"reference={reference_docname} | "
            f"tx_ref={tx_ref}"
        )

        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        }

        # -----------------------------------------------------
        # CUSTOMER EMAIL
        # -----------------------------------------------------

        email = (
            self.data.get("payer_email")
            or frappe.session.user
        )

        if (
            not email
            or email == "Guest"
            or "@" not in email
        ):
            email = "mehariwamlake@gmail.com"

        # -----------------------------------------------------
        # TITLE
        # -----------------------------------------------------

        title = (
            self.data.get("title")
            or "Course Payment"
        )

        title = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            title
        )

        title = title[:16]

        # -----------------------------------------------------
        # DESCRIPTION
        # -----------------------------------------------------

        description = (
            self.data.get("description")
            or "Course Payment"
        )

        description = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            description
        )

        description = description[:49]

        # -----------------------------------------------------
        # CHAPA PAYLOAD
        # -----------------------------------------------------

        payload = {
            "amount": str(self.data.amount),
            "currency": "ETB",

            "email": email,

            "first_name": (
                self.data.get("payer_name")
                or "Customer"
            )[:30],

            "last_name": "",

            "phone_number": (
                self.data.get("phone_number")
                or ""
            ),

            "tx_ref": tx_ref,

            "callback_url": get_url(
                "/api/method/"
                "payments.payment_gateways.doctype."
                "chapa_settings.chapa_settings."
                "verify_payment"
            ),

            "return_url": get_url(
                self.data.get("redirect_to")
                or "/"
            ),

            "customization": {
                "title": title,
                "description": description,
            },
        }

        frappe.logger().info(
            f"CHAPA PAYLOAD: {frappe.as_json(payload)}"
        )

        # -----------------------------------------------------
        # SEND PAYMENT REQUEST
        # -----------------------------------------------------

        try:

            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=30,
            )

            frappe.logger().info(
                f"CHAPA INITIALIZE STATUS: "
                f"{response.status_code}"
            )

            frappe.logger().info(
                f"CHAPA INITIALIZE RESPONSE: "
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

        except requests.RequestException:
            frappe.throw(
                _("Unable to connect to Chapa")
            )


# =============================================================
# CHAPA CALLBACK / PAYMENT VERIFICATION
# =============================================================

@frappe.whitelist(allow_guest=True)
def verify_payment():

    frappe.logger().info(
        f"CHAPA VERIFY CALLED: {frappe.form_dict}"
    )

    # ---------------------------------------------------------
    # GET TRANSACTION REFERENCE
    # ---------------------------------------------------------

    tx_ref = (
        frappe.form_dict.get("tx_ref")
        or frappe.request.args.get("tx_ref")
    )

    if not tx_ref:
        frappe.throw(
            _("Missing Chapa transaction reference")
        )

    frappe.logger().info(
        f"CHAPA VERIFY TX REF: {tx_ref}"
    )

    # ---------------------------------------------------------
    # GET CHAPA SETTINGS
    # ---------------------------------------------------------

    meta = frappe.get_meta("Chapa Settings")

    if meta.issingle:

        settings = frappe.get_single(
            "Chapa Settings"
        )

    else:

        settings_name = frappe.db.get_value(
            "Chapa Settings",
            {"gateway_name": "Chapa"},
            "name",
        )

        if not settings_name:
            frappe.throw(
                _("Chapa Settings configuration was not found")
            )

        settings = frappe.get_doc(
            "Chapa Settings",
            settings_name
        )

    # ---------------------------------------------------------
    # SECRET KEY
    # ---------------------------------------------------------

    secret_key = settings.get_password(
        "secret_key"
    )

    if not secret_key:
        frappe.throw(
            _("Chapa Secret Key is not configured")
        )

    headers = {
        "Authorization": f"Bearer {secret_key}"
    }

    # ---------------------------------------------------------
    # VERIFY WITH CHAPA
    # ---------------------------------------------------------

    try:

        response = requests.get(
            (
                "https://api.chapa.co/v1/"
                f"transaction/verify/{tx_ref}"
            ),
            headers=headers,
            timeout=15,
        )

        frappe.logger().info(
            f"CHAPA VERIFY STATUS: "
            f"{response.status_code}"
        )

        frappe.logger().info(
            f"CHAPA VERIFY RESPONSE: "
            f"{response.text}"
        )

        response.raise_for_status()

        result = response.json()

    except requests.RequestException as e:

        frappe.logger().error(
            f"CHAPA VERIFY ERROR: {str(e)}"
        )

        frappe.throw(
            _("Unable to verify payment with Chapa")
        )

    # ---------------------------------------------------------
    # VERIFY CHAPA RESPONSE
    # ---------------------------------------------------------

    if result.get("status") != "success":

        frappe.throw(
            _("Payment verification failed")
        )

    payment_data = result.get(
        "data",
        {}
    )

    chapa_status = str(
        payment_data.get("status")
        or ""
    ).lower()

    if chapa_status != "success":

        frappe.throw(
            _("Chapa payment was not successful")
        )

    # ---------------------------------------------------------
    # FIND LMS PAYMENT
    # ---------------------------------------------------------

    payment = find_lms_payment(
        tx_ref,
        payment_data
    )

    if not payment:

        frappe.logger().error(
            f"LMS PAYMENT NOT FOUND | tx_ref={tx_ref}"
        )

        frappe.throw(
            _(
                "Payment was verified successfully, "
                "but the LMS Payment could not be found. "
                "Transaction: {0}"
            ).format(tx_ref)
        )

    # ---------------------------------------------------------
    # MARK PAYMENT AS RECEIVED
    # ---------------------------------------------------------

    if not payment.payment_received:

        payment.payment_received = 1

        payment.payment_id = str(
            payment_data.get("id")
            or tx_ref
        )

        # Save Chapa transaction reference if the field exists
        if payment.meta.has_field("chapa_tx_ref"):
            payment.chapa_tx_ref = tx_ref

        payment.save(
            ignore_permissions=True
        )

        frappe.db.commit()

        frappe.logger().info(
            f"CHAPA PAYMENT MARKED PAID | "
            f"LMS Payment={payment.name}"
        )

    else:

        frappe.logger().info(
            f"LMS PAYMENT ALREADY PAID | "
            f"{payment.name}"
        )

    # ---------------------------------------------------------
    # TRIGGER LMS ENROLLMENT
    # ---------------------------------------------------------

    try:

        if hasattr(payment, "on_payment_authorized"):

            payment.on_payment_authorized()

        elif hasattr(payment, "create_enrollment"):

            payment.create_enrollment()

        frappe.db.commit()

        frappe.logger().info(
            f"LMS ENROLLMENT PROCESS COMPLETED | "
            f"Payment={payment.name}"
        )

    except Exception:

        frappe.logger().error(
            "LMS enrollment failed:\n"
            + frappe.get_traceback()
        )

        # Do NOT mark the Chapa payment as failed.
        # Payment is already successfully verified.

    # ---------------------------------------------------------
    # RETURN SUCCESS
    # ---------------------------------------------------------

    return {
        "status": "success",
        "tx_ref": tx_ref,
        "payment": payment.name,
        "payment_received": payment.payment_received,
        "data": payment_data,
    }


# =============================================================
# FIND LMS PAYMENT
# =============================================================

def find_lms_payment(tx_ref, payment_data=None):

    payment_data = payment_data or {}

    # ---------------------------------------------------------
    # 1. DIRECT PAYMENT NAME
    # ---------------------------------------------------------

    references = [
        payment_data.get("tx_ref"),
        payment_data.get("reference"),
        tx_ref,
    ]

    for reference in references:

        if not reference:
            continue

        if frappe.db.exists(
            "LMS Payment",
            reference
        ):

            return frappe.get_doc(
                "LMS Payment",
                reference
            )

    # ---------------------------------------------------------
    # 2. REMOVE RANDOM SUFFIX
    #
    # Example:
    #
    # learn-html-a83f21d9c4
    #
    # becomes:
    #
    # learn-html
    # ---------------------------------------------------------

    base_reference = tx_ref.rsplit(
        "-",
        1
    )[0]

    # Check if it is directly an LMS Payment name
    if frappe.db.exists(
        "LMS Payment",
        base_reference
    ):

        return frappe.get_doc(
            "LMS Payment",
            base_reference
        )

    # ---------------------------------------------------------
    # 3. SEARCH COMMON LMS PAYMENT FIELDS
    # ---------------------------------------------------------

    meta = frappe.get_meta(
        "LMS Payment"
    )

    possible_fields = [
        "course",
        "course_name",
        "reference_docname",
        "reference_document",
        "payment_reference",
        "transaction_reference",
        "tx_ref",
    ]

    for fieldname in possible_fields:

        if not meta.has_field(fieldname):
            continue

        try:

            payment_name = frappe.db.get_value(
                "LMS Payment",
                {
                    fieldname: base_reference
                },
                "name",
            )

            if payment_name:

                return frappe.get_doc(
                    "LMS Payment",
                    payment_name
                )

        except Exception:

            continue

    # ---------------------------------------------------------
    # 4. SEARCH USING CHAPA PAYMENT ID
    # ---------------------------------------------------------

    chapa_id = payment_data.get("id")

    if chapa_id:

        if meta.has_field("payment_id"):

            payment_name = frappe.db.get_value(
                "LMS Payment",
                {
                    "payment_id": str(chapa_id)
                },
                "name",
            )

            if payment_name:

                return frappe.get_doc(
                    "LMS Payment",
                    payment_name
                )

    # ---------------------------------------------------------
    # NOT FOUND
    # ---------------------------------------------------------

    return None