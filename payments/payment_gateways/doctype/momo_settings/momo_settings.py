import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url
from urllib.parse import urlencode

from payments.payment_gateways.doctype.momo_settings.momo_connector import MomoConnector
from payments.utils import erpnext_app_import_guard


class MoMoSettings(Document):
    @property
    def supported_currencies_list(self):
        raw = self.supported_currencies or "UGX,GHS,XAF,ZMW,LRD,RWF,BIF,EUR"
        return [c.strip() for c in raw.split(",") if c.strip()]

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies_list:
            frappe.throw(
                _(
                    "MTN MoMo does not support transactions in currency '{0}'. "
                    "Supported: {1}"
                ).format(currency, ", ".join(self.supported_currencies_list))
            )

    def on_update(self):
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
        create_mode_of_payment("MoMo-" + self.gateway_name, payment_type="Phone")
        callback = (
            frappe.utils.get_url()
            + "/api/method/payments.payment_gateways.doctype.momo_settings"
              ".momo_settings.verify_transaction"
        )
        self.db_set("callback_url", callback, update_modified=False)
        frappe.db.commit()

    def get_payment_url(self, **kwargs):
        kwargs.setdefault("payment_request_name", kwargs.get("order_id", ""))
        return get_url(f"momo_checkout?{urlencode(kwargs)}")

    def request_for_payment(self, **kwargs):
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
            if response and not frappe.db.exists("Integration Request", response["referenceId"]):
                create_request_log(args, "Host", "MoMo", response["referenceId"])
            return response
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MoMo Request to Pay Error")
            frappe.throw(_("MoMo payment initiation failed."), title=_("MoMo Error"))

    def _get_connector(self):
        env = "sandbox" if self.use_sandbox else "production"
        target_env = "sandbox" if self.use_sandbox else self.target_environment
        return MomoConnector(
            env=env,
            api_user_id=self.api_user_id,
            api_key=self.get_password("api_key"),
            subscription_key=self.get_password("subscription_key"),
            target_environment=target_env,
        )

@frappe.whitelist(allow_guest=True)
def verify_transaction(**kwargs):
    data = frappe._dict(kwargs)
    reference_id = data.get("referenceId") or data.get("externalId")

    if not reference_id:
        return {"error": "Missing reference ID"}

    if not frappe.db.exists("Integration Request", reference_id):
        return {"error": "Integration Request not found"}

    integration_request = frappe.get_doc("Integration Request", reference_id)

    if data.get("status") == "SUCCESSFUL":
        original_user = frappe.session.user
        try:
            # 1. Elevate permissions
            frappe.set_user("Administrator")
            
            # 2. Update Integration Request status explicitly for polling
            integration_request.handle_success(data)
            integration_request.db_set("status", "Completed")

            if integration_request.reference_doctype == "Payment Request":
                pr = frappe.get_doc("Payment Request", integration_request.reference_docname)
                if pr.status != "Paid":
                    pr.create_payment_entry()
                    pr.add_comment("Info", f"MoMo Confirmed. ID: {data.get('financialTransactionId')}")

            frappe.db.commit()
        except Exception:
            integration_request.handle_failure(data)
            frappe.log_error(frappe.get_traceback(), "MoMo Callback Error")
        finally:
            frappe.set_user(original_user)
    else:
        integration_request.handle_failure(data)

    return {"status": "processed", "reference_id": reference_id}

@frappe.whitelist()
def generate_payment_url(payment_request_name):
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
        description=f"MTN MoMo Payment for {pr.party_name}",
        payment_gateway=pr.payment_gateway,
        payment_request_name=pr.name,
    )
    frappe.db.set_value("Payment Request", payment_request_name, "payment_url", url)
    frappe.db.commit()
    return url

@frappe.whitelist(allow_guest=True)
def request_for_payment_by_gateway(gateway_name, **kwargs):
    return frappe.get_doc("MoMo Settings", gateway_name).request_for_payment(**kwargs)
