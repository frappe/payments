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

    # ---------------------------------------------------------
    # CREATE REQUEST
    # ---------------------------------------------------------

    def create_request(self, data):
        return self.get_payment_url(**data)

    # ---------------------------------------------------------
    # INITIALIZE CHAPA PAYMENT
    # ---------------------------------------------------------

    def initialize_transaction(self):

        url = "https://api.chapa.co/v1/transaction/initialize"

        secret_key = self.get_password("secret_key")

        if not secret_key:
            frappe.throw(
                _("Chapa Secret Key is not configured")
            )

        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
        }

        # -----------------------------------------------------
        # IMPORTANT
        #
        # LMS currently appears to pass the COURSE name as
        # reference_docname.
        #
        # We keep it inside tx_ref, but also store enough
        # information in the database/logs to identify it.
        # -----------------------------------------------------

        reference_docname = self.data.get("reference_docname")

        if not reference_docname:
            frappe.throw(
                _("Missing payment reference")
            )

        tx_ref = (
            f"{reference_docname}-"
            f"{uuid.uuid4().hex[:10]}"
        )

        frappe.logger().info(
            f"CHAPA INITIALIZE: reference={reference_docname}, "
            f"tx_ref={tx_ref}"
        )

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
            or "Payment"
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

            "return_url": (
                get_url(
                    self.data.redirect_to
                    or "/"
                )
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
# CHAPA CALLBACK / VERIFICATION
# =============================================================

@frappe.whitelist(allow_guest=True)
def verify_payment():

    frappe.logger().info(
        f"CHAPA VERIFY CALLED: {frappe.form_dict}"
    )

    # ---------------------------------------------------------
    # GET TX REF
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

    settings_name = frappe.db.get_value(
        "Chapa Settings",
        {"gateway_name": "Chapa"},
        "name",
    )

    if not settings_name:

        # Since your DocType may be Single,
        # get the Single document as fallback.

        if frappe.get_meta("Chapa Settings").issingle:

            settings = frappe.get_single(
                "Chapa Settings"
            )

        else:

            frappe.throw(
                _("Chapa Settings configuration was not found")
            )

    else:

        settings = frappe.get_doc(
            "Chapa Settings",
            settings_name
        )

    # ---------------------------------------------------------
    # VERIFY TRANSACTION WITH CHAPA
    # ---------------------------------------------------------

    secret_key = settings.get_password(
        "secret_key"
    )

    if not secret_key:
        frappe.throw(
            _("Chapa Secret Key is not configured")
        )

    headers = {
        "Authorization": (
            f"Bearer {secret_key}"
        )
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

        frappe.logger().info(
            f"CHAPA VERIFY HTTP STATUS: "
            f"{response.status_code}"
        )

        frappe.logger().info(
            f"CHAPA VERIFY RAW RESPONSE: "
            f"{response.text}"
        )

        response.raise_for_status()

        result = response.json()

    except requests.RequestException as e:

        frappe.logger().error(
            f"CHAPA VERIFY REQUEST ERROR: {str(e)}"
        )

        frappe.throw(
            _("Unable to verify payment with Chapa")
        )

    # ---------------------------------------------------------
    # CHAPA RESPONSE
    # ---------------------------------------------------------

    if result.get("status") != "success":

        frappe.logger().error(
            f"CHAPA VERIFICATION FAILED: "
            f"{frappe.as_json(result)}"
        )

        frappe.throw(
            _("Payment verification failed")
        )

    payment_data = result.get(
        "data",
        {}
    )

    # ---------------------------------------------------------
    # VERIFY ACTUAL TRANSACTION STATUS
    # ---------------------------------------------------------

    chapa_status = (
        payment_data.get("status")
        or ""
    ).lower()

    if chapa_status != "success":

        frappe.logger().error(
            f"CHAPA PAYMENT STATUS: "
            f"{chapa_status}"
        )

        frappe.throw(
            _("Chapa payment was not successful")
        )

    # ---------------------------------------------------------
    # FIND LMS PAYMENT
    # ---------------------------------------------------------

    payment = find_lms_payment(
        tx_ref=tx_ref,
        payment_data=payment_data
    )

    if not payment:

        frappe.logger().error(
            "CHAPA PAYMENT COULD NOT BE MAPPED "
            f"TO LMS PAYMENT. TX_REF={tx_ref}"
        )

        frappe.throw(
            _(
                "Payment was verified successfully, "
                "but the LMS Payment record could not "
                "be found. Transaction: {0}"
            ).format(tx_ref)
        )

    # ---------------------------------------------------------
    # UPDATE LMS PAYMENT
    # ---------------------------------------------------------

    if not payment.payment_received:

        payment.payment_received = 1

        payment.payment_id = str(
            payment_data.get("id")
            or tx_ref
        )

        # Store transaction reference if field exists
        if hasattr(payment, "chapa_tx_ref"):
            payment.chapa_tx_ref = tx_ref

        payment.save(
            ignore_permissions=True
        )

        frappe.db.commit()

        frappe.logger().info(
            f"CHAPA PAYMENT MARKED PAID: "
            f"{payment.name}"
        )

    else:

        frappe.logger().info(
            f"LMS PAYMENT ALREADY PAID: "
            f"{payment.name}"
        )

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
    # METHOD 1
    #
    # If Chapa has returned a reference that is actually
    # an LMS Payment name.
    # ---------------------------------------------------------

    possible_references = [
        tx_ref,
        payment_data.get("tx_ref"),
        payment_data.get("reference"),
    ]

    for reference in possible_references:

        if not reference:
            continue

        if frappe.db.exists(
            "LMS Payment",
            reference
        ):

            frappe.logger().info(
                f"LMS PAYMENT FOUND DIRECTLY: "
                f"{reference}"
            )

            return frappe.get_doc(
                "LMS Payment",
                reference
            )

    # ---------------------------------------------------------
    # METHOD 2
    #
    # Extract the first part of:
    #
    # learn-html-a83f21d9c4
    #
    # Result:
    #
    # learn-html
    # ---------------------------------------------------------

    base_reference = tx_ref.rsplit(
        "-",
        1
    )[0]

    frappe.logger().info(
        f"CHAPA BASE REFERENCE: "
        f"{base_reference}"
    )

    if frappe.db.exists(
        "LMS Payment",
        base_reference
    ):

        frappe.logger().info(
            f"LMS PAYMENT FOUND BY TX REF: "
            f"{base_reference}"
        )

        return frappe.get_doc(
            "LMS Payment",
            base_reference
        )

    # ---------------------------------------------------------
    # METHOD 3
    #
    # Search LMS Payment fields.
    #
    # This handles the case where reference_docname
    # is the COURSE name instead of LMS Payment name.
    # ---------------------------------------------------------

    meta = frappe.get_meta(
        "LMS Payment"
    )

    candidate_fields = [
        "course",
        "course_name",
        "reference_docname",
        "reference_document",
        "payment_reference",
        "transaction_reference",
        "tx_ref",
    ]

    for fieldname in candidate_fields:

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

                frappe.logger().info(
                    f"LMS PAYMENT FOUND USING "
                    f"{fieldname}: {payment_name}"
                )

                return frappe.get_doc(
                    "LMS Payment",
                    payment_name
                )

        except Exception:

            frappe.logger().warning(
                f"Could not search LMS Payment "
                f"using field {fieldname}"
            )

    # ---------------------------------------------------------
    # METHOD 4
    #
    # Look at recent unpaid LMS payments belonging
    # to the current user.
    #
    # This is a fallback only.
    # ---------------------------------------------------------

    user = frappe.session.user

    if user and user != "Guest":

        user_fields = [
            "owner",
            "user",
            "customer",
            "student",
        ]

        for fieldname in user_fields:

            if not meta.has_field(fieldname):
                continue

            try:

                payment_names = frappe.get_all(
                    "LMS Payment",
                    filters={
                        fieldname: user,
                        "payment_received": 0,
                    },
                    fields=["name"],
                    order_by="creation desc",
                    limit_page_length=5,
                )

                if len(payment_names) == 1:

                    payment_name = (
                        payment_names[0].name
                    )

                    frappe.logger().info(
                        f"LMS PAYMENT FOUND USING "
                        f"CURRENT USER: {payment_name}"
                    )

                    return frappe.get_doc(
                        "LMS Payment",
                        payment_name
                    )

            except Exception:

                frappe.logger().warning(
                    f"Could not search LMS Payment "
                    f"using user field {fieldname}"
                )

    # ---------------------------------------------------------
    # NOTHING FOUND
    # ---------------------------------------------------------

    return None