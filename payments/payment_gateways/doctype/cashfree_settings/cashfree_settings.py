# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt

import json
import frappe
from frappe import _
from frappe.model.document import Document
from frappe.integrations.utils import create_request_log, make_get_request, make_post_request
from frappe.utils import call_hook_method, get_url
from payments.utils import create_payment_gateway

class CashfreeSettings(Document):
    supported_currencies = ["INR"]
    api_version = "2022-09-01"  # Add default API version

    def init_client(self):
        if self.api_key:
            self.secret = self.get_password(fieldname="api_secret", raise_exception=False)

    def validate(self):
        create_payment_gateway("Cashfree")
        call_hook_method("payment_gateway_enabled", gateway="Cashfree")
        if not self.flags.ignore_mandatory:
            self.validate_cashfree_credentials()

    def get_base_url(self):
        """Get Cashfree API base URL based on environment"""
        if hasattr(self, 'use_sandbox') and self.use_sandbox:
            return "https://sandbox.cashfree.com/pg"
        return "https://api.cashfree.com/pg"

    def get_headers(self):
        """Get common headers for Cashfree API calls"""
        return {
            "x-client-id": self.api_key,
            "x-client-secret": self.get_password(fieldname="api_secret", raise_exception=False),
            "x-api-version": self.api_version,
            "Content-Type": "application/json"
        }

    def validate_cashfree_credentials(self):
        if self.api_key and self.api_secret:
            try:
                make_post_request(
                    url=f"{self.get_base_url()}/eligibility/payment_methods",
                    headers=self.get_headers(),
                    data=json.dumps({"queries": {"amount": 100}})
                )
            except Exception:
                frappe.throw(_("Seems API Key or API Secret is wrong!"))

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. Cashfree does not support transactions in currency '{0}'"
                ).format(currency)
            )

    def get_payment_url(self, **kwargs):
        integration_request = create_request_log(kwargs, service_name="Cashfree")
        return get_url(f"./cashfree_checkout?token={integration_request.name}")

    def create_order(self, token):
        self.integration_request = frappe.get_doc("Integration Request", token)
        data = json.loads(self.integration_request.data)
        self.data = frappe._dict(data)

        # Setup payment options
        payment_options = {
            "order_amount": self.data.get("amount"),
            "order_currency": self.data.get("currency", "INR"),
            "customer_details": {
                "customer_id": self.data.get("payer_email"),
                "customer_name": self.data.get("payer_name"),
                "customer_email": self.data.get("payer_email"),
                "customer_phone": self.data.get("payer_phone", "")
            },
            "order_meta": {
                "return_url": f"{get_url()}/api/method/payments.payment_gateways.doctype.cashfree_settings.cashfree_settings.verify_payment?token={token}"
            }
        }
        if self.api_key and self.api_secret:
            try:
                order = make_post_request(
                    f"{self.get_base_url()}/orders",
                    headers=self.get_headers(),
                    data=json.dumps(payment_options)
                )
                self.integration_request.update_status({"payment_options":payment_options, "order_id": order.get('order_id')}, "Queued")
                self.integration_request.db_set("output", json.dumps(order), update_modified=False)
                order["integration_request"] = token
                return order
            except Exception as e:
                frappe.log_error(frappe.get_traceback())
                frappe.throw(_("Could not create Cashfree order"))

    def create_request(self, data):
        self.data = frappe._dict(data)

        try:
            self.integration_request = frappe.get_doc("Integration Request", self.data.token)
            self.integration_request.update_status(self.data, "Queued")
            return self.authorize_payment()

        except Exception:
            frappe.log_error(frappe.get_traceback())
            return {
                "redirect_to": frappe.redirect_to_message(
                    _("Server Error"),
                    _(
                        "Seems issue with server's Cashfree config. Don't worry, in case of failure amount will get refunded to your account."
                    )
                ),
                "status": 401,
            }

    def authorize_payment(self):
        """
        Verify payment status after user completes payment on Cashfree
        """
        data = json.loads(self.integration_request.data)

        try:
            resp = make_get_request(
                f"{self.get_base_url()}/orders/{self.data.order_id}",
                headers=self.get_headers()
            )

            if resp.get("order_status") == "PAID":
                self.integration_request.update_status(data, "Completed")
                self.flags.status_changed_to = "Completed"
            else:
                frappe.log_error(message=str(resp), title="Cashfree Payment not authorized")

        except Exception:
            frappe.log_error()

        status = frappe.flags.integration_request.status_code

        redirect_to = data.get("redirect_to") or None
        redirect_message = data.get("redirect_message") or None

        if self.flags.status_changed_to == "Completed":
            redirect_url = "payment-success"
            if self.data.reference_doctype and self.data.reference_docname:
                redirect_url += f"?doctype={self.data.reference_doctype}&docname={self.data.reference_docname}"
        else:
            redirect_url = "payment-failed"

        if redirect_to:
            redirect_url += f"&redirect_to={redirect_to}"
        if redirect_message:
            redirect_url += f"&redirect_message={redirect_message}"

        return {"redirect_to": redirect_url, "status": status}

    @frappe.whitelist()
    def clear(self):
        self.api_key = self.api_secret = None
        self.redirect_url = None
        self.flags.ignore_mandatory = True
        self.save()

@frappe.whitelist(allow_guest=True)
def get_api_key():
    controller = frappe.get_doc("Cashfree Settings")
    return controller.api_key

@frappe.whitelist(allow_guest=True)
def verify_payment(token):
    """Verify payment callback from Cashfree"""
    integration = frappe.get_doc("Integration Request", token)
    
    data = json.loads(integration.data)
    controller = frappe.get_doc("Cashfree Settings")

    controller.integration_request = integration
    controller.data = frappe._dict(data)

    response = controller.authorize_payment()
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = f"{get_url()}/{response.get('redirect_to')}"
    frappe.local.response["status"] = 200
