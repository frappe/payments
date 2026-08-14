# Copyright (c) 2026, TechVision and contributors
# License: MIT

import re
import uuid
import requests

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

from payments.utils import create_payment_gateway


class ChapaSettings(Document):

    supported_currencies = ("ETB",)

    # ==========================================================
    # SAVE / REGISTER GATEWAY
    # ==========================================================

    def on_update(self):
        create_payment_gateway(
            "Chapa",
            settings=self.name,
            controller="ChapaSettings",
        )

        self.validate_credentials()

    # ==========================================================
    # VALIDATE CHAPA CREDENTIALS
    # ==========================================================

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

    # ==========================================================
    # VALIDATE CURRENCY
    # ==========================================================

    def validate_transaction_currency(self, currency):

        if (
            currency
            and currency not in self.supported_currencies
        ):
            frappe.throw(
                _("Chapa only supports ETB transactions")
            )

    # ==========================================================
    # PAYMENT URL
    # ==========================================================

    def get_payment_url(self, **kwargs):

        self.data = frappe._dict(kwargs)

        self.validate_transaction_currency(
            self.data.get("currency")
        )

        return self.initialize_transaction()

    # ==========================================================
    # CREATE REQUEST
    # ==========================================================

    def create_request(self, data):

        return self.get_payment_url(**data)

    # ==========================================================
    # FIND LMS PAYMENT
    # ==========================================================

    def find_lms_payment(self):

        # ------------------------------------------------------
        # 1. Explicit payment name
        # ------------------------------------------------------

        possible_names = [
            self.data.get("payment_name"),
            self.data.get("payment_id"),
            self.data.get("lms_payment"),
            self.data.get("order_id"),
        ]

        for payment_name in possible_names:

            if not payment_name:
                continue

            if frappe.db.exists(
                "LMS Payment",
                payment_name,
            ):
                return payment_name

        # ------------------------------------------------------
        # 2. Reference document
        # ------------------------------------------------------

        reference = self.data.get(
            "reference_docname"
        )

        if reference:

            if frappe.db.exists(
                "LMS Payment",
                reference,
            ):
                return reference

        # ------------------------------------------------------
        # 3. Try course/source matching
        # ------------------------------------------------------

        course = (
            self.data.get("course")
            or self.data.get("reference_docname")
        )

        user = (
            self.data.get("payer_email")
            or frappe.session.user
        )

        meta = frappe.get_meta(
            "LMS Payment"
        )

        field_names = {
            field.fieldname
            for field in meta.fields
        }

        # ------------------------------------------------------
        # Search by source
        # ------------------------------------------------------

        if (
            course
            and "source" in field_names
        ):

            filters = {
                "source": course
            }

            if user and user != "Guest":
                filters["owner"] = user

            payment_name = frappe.db.get_value(
                "LMS Payment",
                filters,
                "name",
                order_by="creation desc",
            )

            if payment_name:
                return payment_name

        # ------------------------------------------------------
        # Search by course
        # ------------------------------------------------------

        if (
            course
            and "course" in field_names
        ):

            filters = {
                "course": course
            }

            if user and user != "Guest":
                filters["owner"] = user

            payment_name = frappe.db.get_value(
                "LMS Payment",
                filters,
                "name",
                order_by="creation desc",
            )

            if payment_name:
                return payment_name

        # ------------------------------------------------------
        # Search latest payment for current user
        # ------------------------------------------------------

        if frappe.session.user != "Guest":

            payment_name = frappe.db.get_value(
                "LMS Payment",
                {
                    "owner": frappe.session.user
                },
                "name",
                order_by="creation desc",
            )

            if payment_name:
                return payment_name

        return None

    # ==========================================================
    # INITIALIZE CHAPA PAYMENT
    # ==========================================================

    def initialize_transaction(self):

        url = (
            "https://api.chapa.co/"
            "v1/transaction/initialize"
        )

        secret_key = self.get_password(
            "secret_key"
        )

        if not secret_key:

            frappe.throw(
                _("Chapa Secret Key is not configured")
            )

        headers = {
            "Authorization": (
                f"Bearer {secret_key}"
            ),
            "Content-Type": "application/json",
        }

        # ------------------------------------------------------
        # FIND LMS PAYMENT
        # ------------------------------------------------------

        payment_name = self.find_lms_payment()

        if not payment_name:

            frappe.throw(
                _(
                    "Could not find the LMS Payment "
                    "document for this transaction."
                )
            )

        # ------------------------------------------------------
        # UNIQUE CHAPA REFERENCE
        # ------------------------------------------------------

        tx_ref = (
            f"{payment_name}-"
            f"{uuid.uuid4().hex[:10]}"
        )

        # ------------------------------------------------------
        # EMAIL
        # ------------------------------------------------------

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

        # ------------------------------------------------------
        # NAME
        # ------------------------------------------------------

        payer_name = (
            self.data.get("payer_name")
            or "Customer"
        )

        payer_name = payer_name[:30]

        # ------------------------------------------------------
        # TITLE
        # ------------------------------------------------------

        title = (
            self.data.get("title")
            or "Payment for LMS"
        )

        title = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            title,
        )

        title = title[:16]

        # ------------------------------------------------------
        # DESCRIPTION
        # ------------------------------------------------------

        description = (
            self.data.get("description")
            or "LMS Course Payment"
        )

        description = re.sub(
            r"[^A-Za-z0-9_. -]",
            "",
            description,
        )

        description = description[:49]

        # ------------------------------------------------------
        # PAYLOAD
        # ------------------------------------------------------

        payload = {

            "amount": str(
                self.data.amount
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
                "payments.payment_gateways."
                "doctype.chapa_settings."
                "chapa_settings.verify_payment"
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

        # ------------------------------------------------------
        # LOG
        # ------------------------------------------------------

        frappe.logger().info(
            "CHAPA INITIALIZE "
            f"PAYMENT={payment_name} "
            f"TX_REF={tx_ref}"
        )

        # ------------------------------------------------------
        # REQUEST
        # ------------------------------------------------------

        try:

            response = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=30,
            )

            frappe.logger().info(
                f"CHAPA STATUS: "
                f"{response.status_code}"
            )

            frappe.logger().info(
                f"CHAPA RESPONSE: "
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

        except requests.RequestException as e:

            frappe.throw(
                _(
                    "Unable to connect to Chapa: {0}"
                ).format(str(e))
            )

    # ==========================================================
    # VERIFY PAYMENT
    # ==========================================================


@frappe.whitelist(allow_guest=True)
def verify_payment():

    logger = frappe.logger()

    logger.info(
        f"CHAPA VERIFY CALLED: "
        f"{frappe.form_dict}"
    )

    # ----------------------------------------------------------
    # GET TX REF
    # ----------------------------------------------------------

    tx_ref = (
        frappe.form_dict.get("tx_ref")
        or frappe.request.args.get("tx_ref")
    )

    if not tx_ref:

        frappe.throw(
            _("Missing Chapa transaction reference")
        )

    logger.info(
        f"CHAPA TX REF: {tx_ref}"
    )

    # ----------------------------------------------------------
    # GET SETTINGS
    # ----------------------------------------------------------

    settings = frappe.get_doc(
        "Chapa Settings",
        frappe.db.get_value(
            "Chapa Settings",
            {
                "gateway_name": "Chapa"
            },
            "name",
        ),
    )

    if not settings:

        frappe.throw(
            _("Chapa Settings not configured")
        )

    secret_key = settings.get_password(
        "secret_key"
    )

    if not secret_key:

        frappe.throw(
            _("Chapa Secret Key is not configured")
        )

    # ----------------------------------------------------------
    # VERIFY WITH CHAPA
    # ----------------------------------------------------------

    headers = {
        "Authorization": (
            f"Bearer {secret_key}"
        )
    }

    try:

        response = requests.get(
            (
                "https://api.chapa.co/"
                "v1/transaction/verify/"
                f"{tx_ref}"
            ),
            headers=headers,
            timeout=15,
        )

        response.raise_for_status()

    except requests.RequestException as e:

        frappe.log_error(
            frappe.get_traceback(),
            "Chapa Verification Error",
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

    # ----------------------------------------------------------
    # VERIFY API STATUS
    # ----------------------------------------------------------

    if result.get("status") != "success":

        frappe.throw(
            _("Payment verification failed")
        )

    payment_data = result.get(
        "data",
        {}
    )

    # ----------------------------------------------------------
    # VERIFY ACTUAL PAYMENT STATUS
    # ----------------------------------------------------------

    chapa_payment_status = (
        payment_data.get("status")
        or ""
    ).lower()

    if chapa_payment_status != "success":

        frappe.throw(
            _(
                "Chapa payment status is {0}"
            ).format(
                chapa_payment_status
                or "unknown"
            )
        )

    # ----------------------------------------------------------
    # EXTRACT LMS PAYMENT NAME
    #
    # Example:
    #
    # LMS-PAY-00042-d8e41537ea
    #
    # becomes:
    #
    # LMS-PAY-00042
    # ----------------------------------------------------------

    payment_name = tx_ref.rsplit(
        "-",
        1,
    )[0]

    logger.info(
        f"LMS PAYMENT NAME: {payment_name}"
    )

    # ----------------------------------------------------------
    # FIND LMS PAYMENT
    # ----------------------------------------------------------

    if not frappe.db.exists(
        "LMS Payment",
        payment_name,
    ):

        frappe.log_error(
            (
                f"Chapa tx_ref: {tx_ref}\n"
                f"Expected LMS Payment: "
                f"{payment_name}\n"
                f"Chapa response:\n"
                f"{frappe.as_json(payment_data)}"
            ),
            "Chapa LMS Payment Not Found",
        )

        frappe.throw(
            _(
                "Payment was verified successfully, "
                "but LMS Payment {0} was not found."
            ).format(
                payment_name
            )
        )

    # ----------------------------------------------------------
    # LOAD PAYMENT
    # ----------------------------------------------------------

    payment = frappe.get_doc(
        "LMS Payment",
        payment_name,
    )

    # ----------------------------------------------------------
    # MARK PAYMENT RECEIVED
    # ----------------------------------------------------------

    if not payment.payment_received:

        payment.payment_received = 1

        payment.payment_id = str(
            payment_data.get("reference")
            or payment_data.get("id")
            or tx_ref
        )

        payment.save(
            ignore_permissions=True
        )

        frappe.db.commit()

        logger.info(
            f"LMS PAYMENT MARKED PAID: "
            f"{payment.name}"
        )

    # ----------------------------------------------------------
    # ENROLL STUDENT
    # ----------------------------------------------------------

    try:

        if hasattr(
            payment,
            "on_payment_authorized"
        ):

            payment.on_payment_authorized()

        elif hasattr(
            payment,
            "create_enrollment"
        ):

            payment.create_enrollment()

        frappe.db.commit()

    except Exception:

        frappe.log_error(
            frappe.get_traceback(),
            "Chapa LMS Enrollment Error",
        )

        # Payment is already verified.
        # Don't make Chapa think payment failed.

    # ----------------------------------------------------------
    # RESPONSE
    # ----------------------------------------------------------

    return {

        "status": "success",

        "tx_ref": tx_ref,

        "payment": payment.name,

        "payment_received": (
            payment.payment_received
        ),

        "data": payment_data,
    }