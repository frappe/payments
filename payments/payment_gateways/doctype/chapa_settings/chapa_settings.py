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
    # GET PAYMENT URL
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
        #
        # Example:
        #
        # LMS Payment:
        # hogkevj7kq
        #
        # Chapa:
        # hogkevj7kq-a82f92bc12
        # -----------------------------------------------------

        tx_ref = (
            f"{payment.name}-"
            f"{uuid.uuid4().hex[:10]}"
        )

        # Save tx_ref if LMS Payment has a suitable field
        self._save_transaction_reference(
            payment,
            tx_ref,
        )

        # -----------------------------------------------------
        # CUSTOMER EMAIL
        # -----------------------------------------------------

        email = (
            self.data.get("payer_email")
            or payment.member
            or frappe.session.user
        )

        if (
            not email
            or email == "Guest"
            or "@" not in email
        ):
            email = "mehariwamlake@gmail.com"

        # -----------------------------------------------------
        # CUSTOMER NAME
        # -----------------------------------------------------

        payer_name = (
            self.data.get("payer_name")
            or payment.billing_name
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
            or "LMS course payment"
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

        reference_docname = (
            self.data.get("reference_docname")
        )

        user = (
            self.data.get("payer")
            or self.data.get("user")
            or frappe.session.user
        )

        frappe.logger().info(
            "CHAPA FIND LMS PAYMENT: "
            f"reference_docname={reference_docname}, "
            f"user={user}"
        )

        # -----------------------------------------------------
        # METHOD 1
        # reference_docname itself is LMS Payment
        # -----------------------------------------------------

        if reference_docname:

            if frappe.db.exists(
                "LMS Payment",
                reference_docname,
            ):

                return frappe.get_doc(
                    "LMS Payment",
                    reference_docname,
                )

        # -----------------------------------------------------
        # GET LMS PAYMENT META
        # -----------------------------------------------------

        meta = frappe.get_meta(
            "LMS Payment"
        )

        fields = {
            field.fieldname
            for field in meta.fields
        }

        # -----------------------------------------------------
        # METHOD 2
        # Search reference_docname
        # -----------------------------------------------------

        if (
            reference_docname
            and "reference_docname" in fields
        ):

            payment_name = frappe.db.get_value(
                "LMS Payment",
                {
                    "reference_docname":
                        reference_docname
                },
                "name",
            )

            if payment_name:

                return frappe.get_doc(
                    "LMS Payment",
                    payment_name,
                )

        # -----------------------------------------------------
        # METHOD 3
        # Search course
        # -----------------------------------------------------

        if (
            reference_docname
            and "course" in fields
        ):

            payment_name = frappe.db.get_value(
                "LMS Payment",
                {
                    "course":
                        reference_docname
                },
                "name",
            )

            if payment_name:

                return frappe.get_doc(
                    "LMS Payment",
                    payment_name,
                )

        # -----------------------------------------------------
        # METHOD 4
        # Search payment by document + member
        # -----------------------------------------------------

        filters = {}

        if reference_docname:

            if "payment_for_document" in fields:

                filters["payment_for_document"] = (
                    reference_docname
                )

            if "member" in fields and user:

                filters["member"] = user

        if filters:

            payment_name = frappe.db.get_value(
                "LMS Payment",
                filters,
                "name",
                order_by="creation desc",
            )

            if payment_name:

                return frappe.get_doc(
                    "LMS Payment",
                    payment_name,
                )

        return None

    # =========================================================
    # SAVE TRANSACTION REFERENCE
    # =========================================================

    def _save_transaction_reference(
        self,
        payment,
        tx_ref,
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
            "reference",
        ]

        for fieldname in possible_fields:

            if fieldname not in fields:
                continue

            try:

                payment.db_set(
                    fieldname,
                    tx_ref,
                    update_modified=False,
                )

                frappe.logger().info(
                    f"CHAPA TX REF SAVED: "
                    f"{payment.name} -> {tx_ref}"
                )

                return

            except Exception:

                frappe.log_error(
                    frappe.get_traceback(),
                    "Chapa Transaction Reference Save Failed",
                )

    # =========================================================
    # INSTANCE COMPATIBILITY
    # =========================================================

    def verify_payment(self):

        return verify_payment()


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
    # GET CHAPA SETTINGS
    # =========================================================

    settings_name = frappe.db.get_value(
        "Chapa Settings",
        {"gateway_name": "Chapa"},
        "name",
    )

    if settings_name:

        settings = frappe.get_doc(
            "Chapa Settings",
            settings_name,
        )

    else:

        try:

            settings = frappe.get_single(
                "Chapa Settings"
            )

        except Exception:

            frappe.throw(
                _("Chapa Settings configuration was not found.")
            )

    # =========================================================
    # GET SECRET KEY
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
    # VERIFY TRANSACTION WITH CHAPA
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
            "Chapa Verification Request Failed",
        )

        frappe.throw(
            _("Unable to verify payment with Chapa: {0}").format(
                str(e)
            )
        )

    # =========================================================
    # CHECK CHAPA RESPONSE
    # =========================================================

    if result.get("status") != "success":

        frappe.log_error(
            frappe.as_json(result),
            "Chapa Payment Verification Failed",
        )

        frappe.throw(
            _("Chapa payment verification failed.")
        )

    payment_data = (
        result.get("data")
        or {}
    )

    # =========================================================
    # CHECK ACTUAL PAYMENT STATUS
    # =========================================================

    chapa_status = (
        payment_data.get("status")
    )

    if chapa_status != "success":

        frappe.throw(
            _(
                "Payment was not successful. "
                "Chapa status: {0}"
            ).format(
                chapa_status
            )
        )

    # =========================================================
    # FIND LMS PAYMENT
    # =========================================================

    payment = _find_payment_from_tx_ref(
        tx_ref
    )

    # =========================================================
    # FALLBACK TO PAYMENT NAME
    #
    # Example:
    #
    # hogkevj7kq-a82f92bc12
    #
    # Payment:
    # hogkevj7kq
    # =========================================================

    if not payment:

        possible_payment_name = (
            tx_ref.rsplit("-", 1)[0]
        )

        if frappe.db.exists(
            "LMS Payment",
            possible_payment_name,
        ):

            payment = frappe.get_doc(
                "LMS Payment",
                possible_payment_name,
            )

    # =========================================================
    # PAYMENT NOT FOUND
    # =========================================================

    if not payment:

        frappe.log_error(
            frappe.as_json({
                "tx_ref": tx_ref,
                "chapa_response": payment_data,
            }),
            "Chapa Payment - LMS Payment Not Found",
        )

        # Payment is verified by Chapa.
        # Do not report a failed payment to the gateway.

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
    # PAYMENT ALREADY PROCESSED
    # =========================================================

    if payment.payment_received:

        # Even if payment was already marked paid,
        # make sure enrollment exists.

        _complete_lms_enrollment(
            payment
        )

        return {
            "status": "success",
            "verified": True,
            "payment_found": True,
            "already_processed": True,
            "payment": payment.name,
            "tx_ref": tx_ref,
        }

    # =========================================================
    # UPDATE LMS PAYMENT
    # =========================================================

    payment.payment_received = 1

    payment.payment_id = str(
        payment_data.get("reference")
        or payment_data.get("id")
        or tx_ref
    )

    # ---------------------------------------------------------
    # Save tx_ref if field exists
    # ---------------------------------------------------------

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

    # =========================================================
    # SAVE PAYMENT
    # =========================================================

    payment.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    frappe.logger().info(
        f"CHAPA PAYMENT MARKED PAID: "
        f"{payment.name}"
    )

    # =========================================================
    # COMPLETE LMS ENROLLMENT
    # =========================================================

    _complete_lms_enrollment(
        payment
    )

    # =========================================================
    # FINAL RESPONSE
    # =========================================================

    return {
        "status": "success",
        "verified": True,
        "payment_found": True,
        "payment": payment.name,
        "payment_received": payment.payment_received,
        "tx_ref": tx_ref,
        "chapa_reference": payment_data.get(
            "reference"
        ),
        "data": payment_data,
    }


# =============================================================
# COMPLETE LMS ENROLLMENT
# =============================================================

def _complete_lms_enrollment(payment):

    try:

        from lms.lms.utils import complete_enrollment

        # -----------------------------------------------------
        # IMPORTANT
        #
        # Chapa callback runs as Guest.
        #
        # Standard LMS enroll_in_course() uses:
        #
        # frappe.session.user
        #
        # Therefore temporarily switch to the member
        # stored in LMS Payment.
        # -----------------------------------------------------

        member = payment.member

        if not member:

            frappe.log_error(
                f"LMS Payment {payment.name} has no member.",
                "Chapa LMS Enrollment Failed",
            )

            return False

        original_user = frappe.session.user

        try:

            frappe.set_user(
                member
            )

            frappe.logger().info(
                f"CHAPA ENROLLMENT START: "
                f"payment={payment.name}, "
                f"member={member}, "
                f"type={payment.payment_for_document_type}, "
                f"document={payment.payment_for_document}"
            )

            complete_enrollment(
                payment.name,
                payment.payment_for_document_type,
                payment.payment_for_document,
            )

            frappe.db.commit()

            frappe.logger().info(
                f"CHAPA ENROLLMENT SUCCESS: "
                f"payment={payment.name}, "
                f"member={member}, "
                f"document={payment.payment_for_document}"
            )

            return True

        finally:

            # Restore callback user
            frappe.set_user(
                original_user
            )

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "Chapa LMS Enrollment Failed",
        )

        return False


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
    # Search transaction fields
    # ---------------------------------------------------------

    for fieldname in [
        "transaction_reference",
        "tx_ref",
        "transaction_id",
        "reference",
    ]:

        if fieldname not in fields:
            continue

        payment_name = frappe.db.get_value(
            "LMS Payment",
            {
                fieldname: tx_ref
            },
            "name",
        )

        if payment_name:

            return frappe.get_doc(
                "LMS Payment",
                payment_name,
            )

    return None