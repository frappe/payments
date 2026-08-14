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

    # ============================================================
    # REGISTER PAYMENT GATEWAY
    # ============================================================

    def on_update(self):
        create_payment_gateway(
            "Chapa",
            settings=self.name,
            controller="ChapaSettings",
        )

        self.validate_credentials()

    # ============================================================
    # VALIDATE CHAPA CREDENTIALS
    # ============================================================

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
            frappe.throw(_("Unable to connect to Chapa API"))

    # ============================================================
    # VALIDATE CURRENCY
    # ============================================================

    def validate_transaction_currency(self, currency):

        if currency and currency not in self.supported_currencies:
            frappe.throw(
                _("Chapa only supports ETB transactions")
            )

    # ============================================================
    # GET PAYMENT URL
    # ============================================================

    def get_payment_url(self, **kwargs):

        self.data = frappe._dict(kwargs)

        self.validate_transaction_currency(
            self.data.get("currency")
        )

        return self.initialize_transaction()

    # ============================================================
    # CREATE REQUEST
    # ============================================================

    def create_request(self, data):
        return self.get_payment_url(**data)

    # ============================================================
    # INITIALIZE CHAPA TRANSACTION
    # ============================================================

    def initialize_transaction(self):

        url = "https://api.chapa.co/v1/transaction/initialize"

        secret_key = self.get_password("secret_key")

        if not secret_key:
            frappe.throw(_("Chapa Secret Key is not configured"))

        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        }

        # --------------------------------------------------------
        # IMPORTANT:
        #
        # reference_docname can be the COURSE name.
        # We need the actual LMS Payment document name.
        #
        # LMS normally provides:
        #
        # order_id
        # reference_docname
        # payment_name / payment_id
        #
        # We try all possible fields.
        # --------------------------------------------------------

        payment_name = (
            self.data.get("payment_name")
            or self.data.get("payment_id")
            or self.data.get("lms_payment")
            or self.data.get("order_id")
        )

        reference_docname = self.data.get(
            "reference_docname"
        )

        # --------------------------------------------------------
        # If payment_name is an actual LMS Payment document,
        # use it.
        # --------------------------------------------------------

        if payment_name and frappe.db.exists(
            "LMS Payment",
            payment_name
        ):
            payment_reference = payment_name

        else:
            # ----------------------------------------------------
            # Try reference_docname
            # ----------------------------------------------------

            if reference_docname and frappe.db.exists(
                "LMS Payment",
                reference_docname
            ):
                payment_reference = reference_docname

            else:
                # ------------------------------------------------
                # Last fallback:
                # find the latest LMS Payment belonging to the
                # current user/course.
                # ------------------------------------------------

                payment_reference = self.find_lms_payment()

        if not payment_reference:

            frappe.throw(
                _(
                    "Could not find the LMS Payment document "
                    "for this transaction."
                )
            )

        # --------------------------------------------------------
        # Generate Chapa transaction reference
        #
        # Example:
        #
        # LMS-PAY-00001-ca4c80f7bc
        # --------------------------------------------------------

        tx_ref = (
            f"{payment_reference}"
            f"-{uuid.uuid4().hex[:10]}"
        )

        # --------------------------------------------------------
        # CUSTOMER EMAIL
        # --------------------------------------------------------

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

        # --------------------------------------------------------
        # TITLE
        # --------------------------------------------------------

        title = (
            self.data.get("title")
            or "Payment"
        )

        title = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            title
        )

        title = title[:16]

        # --------------------------------------------------------
        # DESCRIPTION
        # --------------------------------------------------------

        description = (
            self.data.get("description")
            or "LMS Course Payment"
        )

        description = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            description
        )

        description = description[:49]

        # --------------------------------------------------------
        # CHAPA PAYLOAD
        # --------------------------------------------------------

        payload = {

            "amount": str(
                self.data.amount
            ),

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
                self.data.redirect_to
                or "/"
            ),

            "customization": {
                "title": title,
                "description": description,
            },
        }

        # --------------------------------------------------------
        # LOG
        # --------------------------------------------------------

        frappe.logger().info(
            "CHAPA INITIALIZE: "
            f"payment={payment_reference}, "
            f"tx_ref={tx_ref}, "
            f"amount={self.data.amount}"
        )

        # --------------------------------------------------------
        # CALL CHAPA
        # --------------------------------------------------------

        try:

            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=30,
            )

            frappe.logger().info(
                f"CHAPA STATUS: {response.status_code}"
            )

            frappe.logger().info(
                f"CHAPA RESPONSE: {response.text}"
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
                    _(
                        "Chapa did not return "
                        "a checkout URL."
                    )
                )

            return checkout_url

        except requests.HTTPError:

            frappe.throw(
                response.text
            )

        except requests.RequestException:

            frappe.throw(
                _(
                    "Unable to connect to Chapa."
                )
            )

    # ============================================================
    # FIND LMS PAYMENT
    # ============================================================

    def find_lms_payment(self):

        # --------------------------------------------------------
        # First try explicit payment fields
        # --------------------------------------------------------

        possible_names = [
            self.data.get("payment_name"),
            self.data.get("payment_id"),
            self.data.get("lms_payment"),
            self.data.get("order_id"),
        ]

        for name in possible_names:

            if name and frappe.db.exists(
                "LMS Payment",
                name
            ):
                return name

        # --------------------------------------------------------
        # Try current user
        # --------------------------------------------------------

        filters = {
            "owner": frappe.session.user,
        }

        # --------------------------------------------------------
        # If course is provided, try source/course field
        # --------------------------------------------------------

        course = (
            self.data.get("reference_docname")
            or self.data.get("course")
        )

        if course:

            meta = frappe.get_meta(
                "LMS Payment"
            )

            field_names = {
                field.fieldname
                for field in meta.fields
            }

            if "source" in field_names:

                payment = frappe.db.get_value(
                    "LMS Payment",
                    {
                        "owner": frappe.session.user,
                        "source": course,
                    },
                    "name",
                    order_by="creation desc",
                )

                if payment:
                    return payment

            if "course" in field_names:

                payment = frappe.db.get_value(
                    "LMS Payment",
                    {
                        "owner": frappe.session.user,
                        "course": course,
                    },
                    "name",
                    order_by="creation desc",
                )

                if payment:
                    return payment

        # --------------------------------------------------------
        # Last fallback: latest payment for current user
        # --------------------------------------------------------

        payment = frappe.db.get_value(
            "LMS Payment",
            {
                "owner": frappe.session.user,
            },
            "name",
            order_by="creation desc",
        )

        return payment

    # ============================================================
    # VERIFY PAYMENT
    # ============================================================


@frappe.whitelist(allow_guest=True)
def verify_payment():

    logger = frappe.logger()

    logger.info(
        f"CHAPA VERIFY CALLED: {frappe.form_dict}"
    )

    # ------------------------------------------------------------
    # GET TX REF
    # ------------------------------------------------------------

    tx_ref = (
        frappe.form_dict.get("tx_ref")
        or frappe.request.args.get("tx_ref")
    )

    if not tx_ref:

        frappe.throw(
            _("Missing Chapa transaction reference.")
        )

    logger.info(
        f"CHAPA TX REF: {tx_ref}"
    )

    # ------------------------------------------------------------
    # GET SETTINGS
    # ------------------------------------------------------------

    settings_name = frappe.db.get_value(
        "Chapa Settings",
        {"gateway_name": "Chapa"},
        "name",
    )

    if not settings_name:

        # fallback for Single DocType
        try:

            settings = frappe.get_single(
                "Chapa Settings"
            )

        except Exception:

            frappe.throw(
                _(
                    "Chapa Settings configuration "
                    "was not found."
                )
            )

    else:

        settings = frappe.get_doc(
            "Chapa Settings",
            settings_name
        )

    # ------------------------------------------------------------
    # VERIFY WITH CHAPA
    # ------------------------------------------------------------

    secret_key = settings.get_password(
        "secret_key"
    )

    if not secret_key:

        frappe.throw(
            _("Chapa Secret Key is not configured.")
        )

    headers = {
        "Authorization": f"Bearer {secret_key}"
    }

    try:

        response = requests.get(
            (
                "https://api.chapa.co/v1/"
                f"transaction/verify/{tx_ref}"
            ),
            headers=headers,
            timeout=15,
        )

        response.raise_for_status()

    except requests.RequestException as e:

        frappe.log_error(
            frappe.get_traceback(),
            "Chapa Verification Request Failed",
        )

        frappe.throw(
            _(
                "Unable to verify payment with Chapa: {0}"
            ).format(str(e))
        )

    result = response.json()

    logger.info(
        f"CHAPA VERIFY RESPONSE: {result}"
    )

    # ------------------------------------------------------------
    # CHAPA RESPONSE STATUS
    # ------------------------------------------------------------

    if result.get("status") != "success":

        frappe.throw(
            _("Payment verification failed.")
        )

    payment_data = result.get(
        "data",
        {}
    )

    # ------------------------------------------------------------
    # PAYMENT MUST ACTUALLY BE SUCCESSFUL
    # ------------------------------------------------------------

    chapa_status = (
        payment_data.get("status")
        or ""
    ).lower()

    if chapa_status != "success":

        frappe.throw(
            _(
                "Chapa payment status is {0}."
            ).format(
                chapa_status or "unknown"
            )
        )

    # ------------------------------------------------------------
    # EXTRACT LMS PAYMENT NAME
    #
    # tx_ref:
    #
    # LMS-PAY-00001-ca4c80f7bc
    #
    # Remove ONLY the final UUID portion.
    # ------------------------------------------------------------

    payment_name = tx_ref.rsplit(
        "-",
        1
    )[0]

    logger.info(
        f"CHAPA LMS PAYMENT: {payment_name}"
    )

    # ------------------------------------------------------------
    # FIND LMS PAYMENT
    # ------------------------------------------------------------

    if not frappe.db.exists(
        "LMS Payment",
        payment_name
    ):

        # --------------------------------------------------------
        # Try Chapa response metadata
        # --------------------------------------------------------

        possible_payment_name = (
            payment_data.get("payment_name")
            or payment_data.get("reference")
            or payment_data.get("tx_ref")
        )

        if (
            possible_payment_name
            and frappe.db.exists(
                "LMS Payment",
                possible_payment_name
            )
        ):

            payment_name = (
                possible_payment_name
            )

        else:

            frappe.log_error(
                (
                    f"Transaction: {tx_ref}\n"
                    f"Expected LMS Payment: "
                    f"{payment_name}\n"
                    f"Chapa Data: "
                    f"{frappe.as_json(payment_data)}"
                ),
                "Chapa LMS Payment Not Found",
            )

            frappe.throw(
                _(
                    "Payment was verified successfully, "
                    "but the LMS Payment record could not "
                    "be found. Transaction: {0}"
                ).format(tx_ref)
            )

    # ------------------------------------------------------------
    # LOAD LMS PAYMENT
    # ------------------------------------------------------------

    payment = frappe.get_doc(
        "LMS Payment",
        payment_name
    )

    # ------------------------------------------------------------
    # ALREADY PROCESSED
    # ------------------------------------------------------------

    if payment.payment_received:

        logger.info(
            f"LMS PAYMENT ALREADY PAID: {payment.name}"
        )

        return {
            "status": "success",
            "tx_ref": tx_ref,
            "payment": payment.name,
            "payment_received": 1,
            "already_processed": 1,
            "data": payment_data,
        }

    # ------------------------------------------------------------
    # MARK PAYMENT AS RECEIVED
    # ------------------------------------------------------------

    payment.payment_received = 1

    payment.payment_id = str(
        payment_data.get("id")
        or tx_ref
    )

    payment.save(
        ignore_permissions=True
    )

    # ------------------------------------------------------------
    # COMMIT
    # ------------------------------------------------------------

    frappe.db.commit()

    logger.info(
        f"CHAPA PAYMENT MARKED PAID: {payment.name}"
    )

    # ------------------------------------------------------------
    # IMPORTANT:
    #
    # LMS may have its own enrollment logic.
    # Trigger it if the method exists.
    # ------------------------------------------------------------

    try:

        if hasattr(
            payment,
            "on_payment_authorized"
        ):

            payment.on_payment_authorized()

        if hasattr(
            payment,
            "create_enrollment"
        ):

            payment.create_enrollment()

        frappe.db.commit()

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "Chapa LMS Enrollment Failed",
        )

        # Payment is already verified.
        # Do NOT tell Chapa that payment failed.

    # ------------------------------------------------------------
    # RETURN
    # ------------------------------------------------------------

    return {
        "status": "success",
        "tx_ref": tx_ref,
        "payment": payment.name,
        "payment_received": payment.payment_received,
        "data": payment_data,
    }