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

from lms.lms.utils import complete_enrollment


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
                frappe.throw(
                    _("Invalid Chapa Secret Key")
                )

        except requests.RequestException:
            frappe.throw(
                _("Unable to connect to Chapa API")
            )

    # =========================================================
    # VALIDATE CURRENCY
    # =========================================================

    def validate_transaction_currency(self, currency):

        if currency and currency not in self.supported_currencies:
            frappe.throw(
                _("Chapa only supports ETB transactions")
            )

    # =========================================================
    # PAYMENT ENTRY POINT
    # =========================================================

    def get_payment_url(self, **kwargs):

        self.data = frappe._dict(kwargs)

        self.validate_transaction_currency(
            self.data.get("currency")
        )

        return self.initialize_transaction()

    # =========================================================
    # COMPATIBILITY
    # =========================================================

    def create_request(self, data):

        return self.get_payment_url(**data)

    # =========================================================
    # INITIALIZE CHAPA PAYMENT
    # =========================================================

    def initialize_transaction(self):

        url = (
            "https://api.chapa.co/v1/transaction/initialize"
        )

        headers = {
            "Authorization": (
                f"Bearer {self.get_password('secret_key')}"
            ),
            "Content-Type": "application/json",
        }

        # -----------------------------------------------------
        # FIND LMS PAYMENT
        # -----------------------------------------------------

        payment = self._find_lms_payment()

        if not payment:

            frappe.throw(
                _(
                    "Could not find the LMS Payment document "
                    "for this transaction."
                )
            )

        # -----------------------------------------------------
        # CREATE UNIQUE CHAPA TX REF
        # -----------------------------------------------------

        tx_ref = (
            f"{payment.name}-"
            f"{uuid.uuid4().hex[:10]}"
        )

        # -----------------------------------------------------
        # SAVE TX REF TO LMS PAYMENT
        # -----------------------------------------------------

        self._save_transaction_reference(
            payment,
            tx_ref
        )

        # -----------------------------------------------------
        # EMAIL
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
        # NAME
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
            str(title)
        )

        title = title[:16]

        # -----------------------------------------------------
        # DESCRIPTION
        # -----------------------------------------------------

        description = (
            self.data.get("description")
            or "LMS course payment"
        )

        description = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            str(description)
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
            f"CHAPA INITIALIZE PAYMENT: {payload}"
        )

        # -----------------------------------------------------
        # SEND TO CHAPA
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
                    _("Chapa did not return a checkout URL.")
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

    # =========================================================
    # FIND LMS PAYMENT
    # =========================================================

    def _find_lms_payment(self):

        # -----------------------------------------------------
        # BEST METHOD
        #
        # LMS sends:
        #
        # "payment": payment.name
        # -----------------------------------------------------

        payment_name = self.data.get("payment")

        if payment_name:

            if frappe.db.exists(
                "LMS Payment",
                payment_name
            ):

                return frappe.get_doc(
                    "LMS Payment",
                    payment_name
                )

        # -----------------------------------------------------
        # FALLBACK
        # -----------------------------------------------------

        reference_docname = (
            self.data.get("reference_docname")
        )

        member = (
            self.data.get("payer")
            or self.data.get("user")
            or frappe.session.user
        )

        if not reference_docname:
            return None

        # -----------------------------------------------------
        # SEARCH LMS PAYMENT USING STANDARD LMS FIELDS
        # -----------------------------------------------------

        filters = {
            "payment_for_document": reference_docname,
        }

        meta = frappe.get_meta(
            "LMS Payment"
        )

        fieldnames = {
            field.fieldname
            for field in meta.fields
        }

        if "member" in fieldnames and member:
            filters["member"] = member

        payment_name = frappe.db.get_value(
            "LMS Payment",
            filters,
            "name",
            order_by="creation desc"
        )

        if payment_name:

            return frappe.get_doc(
                "LMS Payment",
                payment_name
            )

        return None

    # =========================================================
    # SAVE TX REF
    # =========================================================

    def _save_transaction_reference(
        self,
        payment,
        tx_ref
    ):

        meta = frappe.get_meta(
            "LMS Payment"
        )

        fields = {
            field.fieldname
            for field in meta.fields
        }

        possible_fields = [
            "transaction_reference",
            "tx_ref",
            "transaction_id",
        ]

        for fieldname in possible_fields:

            if fieldname in fields:

                try:

                    payment.db_set(
                        fieldname,
                        tx_ref,
                        update_modified=False
                    )

                    return

                except Exception:

                    frappe.log_error(
                        frappe.get_traceback(),
                        "Chapa TX Ref Save Failed"
                    )


# =============================================================
# CHAPA CALLBACK / VERIFICATION
# =============================================================

@frappe.whitelist(allow_guest=True)
def verify_payment():

    frappe.logger().info(
        f"CHAPA VERIFY CALLED: {frappe.form_dict}"
    )

    # =========================================================
    # GET TX REF
    # =========================================================

    tx_ref = (
        frappe.form_dict.get("tx_ref")
        or frappe.request.args.get("tx_ref")
    )

    if not tx_ref:

        frappe.throw(
            _("Missing Chapa transaction reference.")
        )

    frappe.logger().info(
        f"CHAPA TX REF: {tx_ref}"
    )

    # =========================================================
    # GET SETTINGS
    # =========================================================

    try:

        settings = frappe.get_single(
            "Chapa Settings"
        )

    except Exception:

        settings_name = frappe.db.get_value(
            "Chapa Settings",
            {"gateway_name": "Chapa"},
            "name"
        )

        if not settings_name:

            frappe.throw(
                _("Chapa Settings configuration was not found.")
            )

        settings = frappe.get_doc(
            "Chapa Settings",
            settings_name
        )

    # =========================================================
    # SECRET KEY
    # =========================================================

    secret_key = settings.get_password(
        "secret_key"
    )

    if not secret_key:

        frappe.throw(
            _("Chapa Secret Key is not configured.")
        )

    headers = {
        "Authorization": (
            f"Bearer {secret_key}"
        )
    }

    # =========================================================
    # VERIFY WITH CHAPA
    # =========================================================

    verify_url = (
        "https://api.chapa.co/v1/"
        f"transaction/verify/{tx_ref}"
    )

    try:

        response = requests.get(
            verify_url,
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

        frappe.log_error(
            frappe.get_traceback(),
            "Chapa Verification Request Failed"
        )

        frappe.throw(
            _("Unable to verify payment with Chapa: {0}").format(
                str(e)
            )
        )

    # =========================================================
    # CHAPA RESPONSE
    # =========================================================

    if result.get("status") != "success":

        frappe.log_error(
            frappe.as_json(result),
            "Chapa Verification Failed"
        )

        frappe.throw(
            _("Chapa payment verification failed.")
        )

    payment_data = (
        result.get("data")
        or {}
    )

    # =========================================================
    # ACTUAL PAYMENT STATUS
    # =========================================================

    if payment_data.get("status") != "success":

        frappe.throw(
            _(
                "Payment was not successful. "
                "Chapa status: {0}"
            ).format(
                payment_data.get("status")
            )
        )

    # =========================================================
    # FIND LMS PAYMENT
    # =========================================================

    payment = _find_payment_from_tx_ref(
        tx_ref
    )

    if not payment:

        # -----------------------------------------------------
        # FALLBACK:
        #
        # learn-html-d8e41537ea
        #
        # Previously we were incorrectly assuming that
        # "learn-html" was the LMS Payment name.
        # It isn't.
        #
        # Therefore search using transaction reference first.
        # -----------------------------------------------------

        frappe.log_error(
            frappe.as_json({
                "tx_ref": tx_ref,
                "chapa_response": payment_data,
            }),
            "Chapa Payment - LMS Payment Not Found"
        )

        return {
            "status": "success",
            "verified": True,
            "payment_found": False,
            "tx_ref": tx_ref,
            "message": (
                "Payment verified successfully, "
                "but LMS Payment was not found."
            ),
        }

    # =========================================================
    # SAVE PAYMENT
    # =========================================================

    if not payment.payment_received:

        payment.payment_received = 1

        payment.payment_id = str(
            payment_data.get("reference")
            or payment_data.get("id")
            or tx_ref
        )

        meta = frappe.get_meta(
            "LMS Payment"
        )

        fields = {
            field.fieldname
            for field in meta.fields
        }

        if "transaction_reference" in fields:

            payment.transaction_reference = tx_ref

        elif "tx_ref" in fields:

            payment.tx_ref = tx_ref

        payment.save(
            ignore_permissions=True
        )

        frappe.db.commit()

    # =========================================================
    # GET PAYMENT TARGET
    # =========================================================

    doctype = payment.payment_for_document_type
    docname = payment.payment_for_document

    if not doctype or not docname:

        frappe.log_error(
            frappe.as_json({
                "payment": payment.name,
                "doctype": doctype,
                "docname": docname,
            }),
            "Chapa Enrollment - Missing Payment Target"
        )

        return {
            "status": "success",
            "verified": True,
            "payment_found": True,
            "payment": payment.name,
            "enrollment": False,
            "message": (
                "Payment verified, but enrollment target "
                "is missing."
            ),
        }

    # =========================================================
    # ENROLLMENT
    # =========================================================

    enrollment_result = None

    try:

        frappe.logger().info(
            "CHAPA LMS ENROLLMENT START: "
            f"payment={payment.name}, "
            f"doctype={doctype}, "
            f"docname={docname}"
        )

        enrollment_result = complete_enrollment(
            payment.name,
            doctype,
            docname
        )

        frappe.db.commit()

        frappe.logger().info(
            "CHAPA LMS ENROLLMENT SUCCESS: "
            f"payment={payment.name}, "
            f"result={enrollment_result}"
        )

    except Exception:

        frappe.db.rollback()

        frappe.log_error(
            frappe.get_traceback(),
            "Chapa LMS Enrollment Failed"
        )

        # Payment itself remains verified.
        # The enrollment error is logged so it can be
        # retried/debugged without charging the customer again.

        return {
            "status": "success",
            "verified": True,
            "payment_found": True,
            "payment": payment.name,
            "payment_received": 1,
            "enrollment": False,
            "doctype": doctype,
            "docname": docname,
            "message": (
                "Payment verified successfully, "
                "but enrollment failed. "
                "Check Error Log."
            ),
        }

    # =========================================================
    # SUCCESS
    # =========================================================

    return {
        "status": "success",
        "verified": True,
        "payment_found": True,
        "payment": payment.name,
        "payment_received": payment.payment_received,
        "enrollment": True,
        "doctype": doctype,
        "docname": docname,
        "tx_ref": tx_ref,
        "chapa_reference": payment_data.get(
            "reference"
        ),
        "data": payment_data,
    }


# =============================================================
# FIND PAYMENT FROM TX REF
# =============================================================

def _find_payment_from_tx_ref(tx_ref):

    meta = frappe.get_meta(
        "LMS Payment"
    )

    fields = {
        field.fieldname
        for field in meta.fields
    }

    # ---------------------------------------------------------
    # MOST RELIABLE: transaction_reference
    # ---------------------------------------------------------

    for fieldname in [
        "transaction_reference",
        "tx_ref",
        "transaction_id",
    ]:

        if fieldname not in fields:
            continue

        payment_name = frappe.db.get_value(
            "LMS Payment",
            {
                fieldname: tx_ref
            },
            "name"
        )

        if payment_name:

            return frappe.get_doc(
                "LMS Payment",
                payment_name
            )

    # ---------------------------------------------------------
    # FALLBACK: payment name is first part of tx_ref
    # ---------------------------------------------------------

    possible_payment_name = (
        tx_ref.rsplit("-", 1)[0]
    )

    if frappe.db.exists(
        "LMS Payment",
        possible_payment_name
    ):

        return frappe.get_doc(
            "LMS Payment",
            possible_payment_name
        )

    return None