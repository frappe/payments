"""
MoMo Settings — DocType controller for MTN Mobile Money gateway.

Architecture:
  Mirrors mpesa_settings.py (on_update, request_for_payment, create_mode_of_payment)
  and gocardless_settings.py (validate_transaction_currency, get_payment_url).

  Key hook into payments.utils:
    create_payment_gateway("MoMo-" + self.gateway_name, ...)
    → inserts a Payment Gateway doc that points back to this Settings doc,
      so get_payment_gateway_controller("MoMo-Uganda") returns
      frappe.get_doc("MoMo Settings", "Uganda").
"""

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_request_site_address, get_url
from urllib.parse import urlencode

from payments.payment_gateways.doctype.momo_settings.momo_connector import MomoConnector
from payments.utils import erpnext_app_import_guard


class MoMoSettings(Document):
    """
    Controller for the 'MoMo Settings' DocType.

    One document per MTN MoMo gateway instance (one per country/environment).
    The document name equals gateway_name (autoname: field:gateway_name).

    Example document:
        gateway_name       = "Uganda"
        api_user_id        = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        api_key            = "••••••••••••"
        subscription_key   = "••••••••••••••••••••••••••••••••"
        target_environment = "mtnuganda"
        use_sandbox        = 0
        supported_currencies = "UGX"
    """

    # ── Currency validation ──────────────────────────────────────────

    @property
    def supported_currencies_list(self):
        """Return the supported_currencies field as a clean list."""
        raw = self.supported_currencies or "UGX,GHS,XAF,ZMW,LRD,RWF,BIF,EUR"
        return [c.strip() for c in raw.split(",") if c.strip()]

    def validate_transaction_currency(self, currency):
        """
        Called by ERPNext Payment Request before rendering the payment URL.
        Mirrors GoCardless pattern from gocardless_settings.py.
        """
        if currency not in self.supported_currencies_list:
            frappe.throw(
                _(
                    "Please select another payment method. "
                    "MTN MoMo does not support transactions in currency '{0}'. "
                    "Supported: {1}"
                ).format(currency, ", ".join(self.supported_currencies_list))
            )

    # ── Gateway registration ─────────────────────────────────────────

    def on_update(self):
        """
        Registers this gateway in the Payment Gateway DocType registry and
        creates a Mode of Payment entry.

        Mirrors mpesa_settings.py on_update() exactly.

        After this runs, get_payment_gateway_controller("MoMo-Uganda") works.
        """
        from payments.utils import create_payment_gateway

        create_payment_gateway(
            "MoMo-" + self.gateway_name,
            settings="MoMo Settings",
            controller=self.gateway_name,
        )

        call_hook_method(
            "payment_gateway_enabled",
            gateway="MoMo-" + self.gateway_name,
            payment_channel="",
        )

        create_mode_of_payment(
            "MoMo-" + self.gateway_name,
            payment_type="Phone",
        )

        callback = (
            frappe.utils.get_url()
            + "/api/method/payments.payment_gateways.doctype.momo_settings"
            ".momo_settings.verify_transaction"
        )

        self.db_set("callback_url", callback, update_modified=False)

        frappe.db.commit()

    # ── Checkout URL ─────────────────────────────────────────────────

    def get_payment_url(self, **kwargs):
        """
        Returns the URL of the MoMo checkout page, with all payment parameters
        encoded as query-string arguments.

        Mirrors GoCardless pattern:
          return get_url(f"gocardless_checkout?{urlencode(kwargs)}")
        """
        kwargs.setdefault("payment_request_name", kwargs.get("order_id", ""))
        return get_url(f"momo_checkout?{urlencode(kwargs)}")

    # ── Payment initiation ────────────────────────────────────────────

    def request_for_payment(self, **kwargs):
        """
        Initiates a Request-to-Pay via MomoConnector and creates an
        Integration Request record to track the transaction.

        Expected kwargs (populated by momo_checkout.html form post):
          - request_amount
          - currency
          - sender
          - order_id
          - reference_doctype / reference_docname

        Returns:
          {"referenceId": "", "status": "PENDING"}
        """

        args = frappe._dict(kwargs)

        try:
            callback_url = (
                frappe.utils.get_url()
                + "/api/method/payments.payment_gateways.doctype.momo_settings"
                ".momo_settings.verify_transaction"
            )

            connector = self._get_connector()

            response = connector.request_to_pay(
                amount=args.request_amount,
                currency=args.currency,
                payer_msisdn=args.sender,
                external_id=args.order_id,
                callback_url=callback_url,
            )

            if response:
                if not frappe.db.exists("Integration Request", response["referenceId"]):
                    create_request_log(args, "Host", "MoMo", response["referenceId"])

            return response

        except Exception:
            frappe.log_error("MoMo Request to Pay Error")

            frappe.throw(
                _("MoMo payment initiation failed. Check the error log for details."),
                title=_("MoMo Error"),
            )

    # ── Internal helpers ──────────────────────────────────────────────

    def _get_connector(self):
        """Instantiate MomoConnector using stored (decrypted) credentials."""

        env = "sandbox" if self.use_sandbox else "production"
        target_env = "sandbox" if self.use_sandbox else self.target_environment

        return MomoConnector(
            env=env,
            api_user_id=self.api_user_id,
            api_key=self.get_password("api_key"),
            subscription_key=self.get_password("subscription_key"),
            target_environment=target_env,
        )


# ════════════════════════ Whitelisted API methods ══════════════════════════


@frappe.whitelist(allow_guest=True)
def verify_transaction(**kwargs):
    """
    Callback endpoint — MTN MoMo POSTs here after a payment attempt.

    URL:
    /api/method/payments.payment_gateways.doctype.momo_settings
                       .momo_settings.verify_transaction
    """

    data = frappe._dict(kwargs)

    reference_id = data.get("referenceId") or data.get("externalId")

    if not reference_id or not isinstance(reference_id, str):
        frappe.throw(_("MoMo callback: invalid or missing reference ID"))

    if not frappe.db.exists("Integration Request", reference_id):
        frappe.log_error(
            f"MoMo: Integration Request not found for reference_id={reference_id}",
            "MoMo Callback Error",
        )

        return {
            "error": "Integration Request not found",
            "reference_id": reference_id,
        }

    integration_request = frappe.get_doc("Integration Request", reference_id)

    if data.get("status") == "SUCCESSFUL":
        try:
            integration_request.handle_success(data)

            if (
                integration_request.reference_doctype
                and integration_request.reference_docname
            ):
                doc = frappe.get_doc(
                    integration_request.reference_doctype,
                    integration_request.reference_docname,
                )

                doc.run_method("on_payment_authorized", "Completed")

        except Exception:
            integration_request.handle_failure(data)
            frappe.log_error(
                "MoMo: Failed to finalize transaction",
                "MoMo Error",
            )
    else:
        integration_request.handle_failure(data)

    return {
        "status": "processed",
        "reference_id": reference_id,
    }


@frappe.whitelist(allow_guest=True)
def poll_transaction_status(reference_id, gateway_name):
    """
    Manually poll the MTN API for the current status of a pending transaction.
    """

    if not isinstance(reference_id, str):
        frappe.throw(_("Invalid reference_id: must be a string"))

    settings = frappe.get_doc("MoMo Settings", gateway_name)
    connector = settings._get_connector()

    status_response = connector.get_transaction_status(reference_id)

    if status_response.get("status") == "SUCCESSFUL":
        verify_transaction(**status_response, referenceId=reference_id)

    return status_response


def poll_pending_transactions():
    """
    Scheduled job (hourly) — polls MTN for all Integration Requests
    that are still in Queued/Pending state for the MoMo gateway.
    """

    pending = frappe.get_all(
        "Integration Request",
        filters={
            "integration_request_service": "MoMo",
            "status": ["in", ["Queued", "Authorized"]],
        },
        fields=["name", "data"],
    )

    for req in pending:
        try:
            import json

            data = json.loads(req.data or "{}")

            gateway_name = data.get("gateway_name") or data.get(
                "payment_gateway", ""
            ).replace("MoMo-", "")

            if not gateway_name:
                continue

            poll_transaction_status(
                reference_id=req.name,
                gateway_name=gateway_name,
            )

        except Exception:
            frappe.log_error(
                f"MoMo: Error polling {req.name}",
                "MoMo Scheduler",
            )


# ════════════════════════ Helpers ═════════════════════════════════════════


def create_mode_of_payment(gateway, payment_type="General"):
    """
    Creates a Mode of Payment entry for this gateway if one does not
    already exist. Mirrors the same function in mpesa_settings.py.
    """

    with erpnext_app_import_guard():
        from erpnext import get_default_company

    payment_gateway_account = frappe.db.get_value(
        "Payment Gateway Account",
        {"payment_gateway": gateway},
        "payment_account",
    )

    if frappe.db.exists("Mode of Payment", gateway):
        return

    if payment_gateway_account:
        mode_of_payment = frappe.get_doc(
            {
                "doctype": "Mode of Payment",
                "mode_of_payment": gateway,
                "enabled": 1,
                "type": payment_type,
                "accounts": [
                    {
                        "doctype": "Mode of Payment Account",
                        "company": get_default_company(),
                        "default_account": payment_gateway_account,
                    }
                ],
            }
        )

        mode_of_payment.insert(ignore_permissions=True)



@frappe.whitelist(allow_guest=True)
def request_for_payment_by_gateway(gateway_name, **kwargs):
    """Standalone wrapper called from momo_checkout.html"""
    settings = frappe.get_doc("MoMo Settings", gateway_name)
    return settings.request_for_payment(**kwargs)


@frappe.whitelist()
def generate_payment_url(payment_request_name):
    """Generate MoMo checkout URL for a Payment Request — called from desk button."""
    pr = frappe.get_doc("Payment Request", payment_request_name)
    from payments.utils import get_payment_gateway_controller
    controller = get_payment_gateway_controller(pr.payment_gateway)
    url = controller.get_payment_url(
        amount=pr.grand_total,
        currency=pr.currency,
        order_id=pr.name,
        reference_doctype="Payment Request",
        reference_docname=pr.name,
        payer_name=pr.party_name,
        payer_email=pr.party,
        title=f"Payment for {pr.reference_name}",
        description=f"Payment for {pr.reference_name}",
        payment_gateway=pr.payment_gateway,
        payment_request_name=pr.name
    )
    frappe.db.set_value("Payment Request", payment_request_name, "payment_url", url)
    frappe.db.commit()
    return url
