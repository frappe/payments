# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe, requests
from frappe.model.document import Document
from frappe.utils import get_request_site_address
from payments.payment_gateways.api.m_pesa_api import get_token

class MpesaC2BPaymentRegisterURL(Document):
    def validate(self):
        sandbox_url = "https://sandbox.safaricom.co.ke"
        live_url = "https://api.safaricom.co.ke"
        mpesa_settings = frappe.get_doc("Mpesa Settings", self.mpesa_settings)
        env = "production" if not mpesa_settings.sandbox else "sandbox"
        business_shortcode = (
            mpesa_settings.business_shortcode
            if env == "production"
            else mpesa_settings.till_number
        )
        if env == "sandbox":
            base_url = sandbox_url
        else:
            base_url = live_url

        token = get_token(
            app_key=mpesa_settings.consumer_key,
            app_secret=mpesa_settings.get_password("consumer_secret"),
            base_url=base_url,
        )

        #site_url = get_request_site_address(True)
        validation_url = (
            #site_url + "/api/method/payments.payment_gateways.doctype.mpesa_c2b_payment_register_url.mpesa_api.validation"
            "https://807f-196-216-86-77.ngrok-free.app"+ "/api/method/payments.payment_gateways.api.m_pesa_api.validation"
        )
        confirmation_url = (
            # site_url + "/api/method/payments.payment_gateways.doctype.mpesa_c2b_payment_register_url.mpesa_api.confirmation"
            "https://807f-196-216-86-77.ngrok-free.app"+ "/api/method/payments.payment_gateways.m_pesa_api.confirmation"
        )
        register_url = base_url + "/mpesa/c2b/v2/registerurl"

        payload = {
            "ShortCode": '8925326',
            "ResponseType": "Completed",
            "ConfirmationURL": confirmation_url,
            "ValidationURL": validation_url,
        }
        headers = {
            "Authorization": "Bearer {0}".format(token),
            "Content-Type": "application/json",
        }

        try:
            r = requests.post(register_url, headers=headers, json=payload)
            r.raise_for_status()  # Raise an HTTPError for bad responses
            res = r.json()
            if res.get("ResponseDescription") == "Success":
                self.register_status = "Success"
            else:
                self.register_status = "Failed"
                frappe.msgprint(str(res))
        except requests.exceptions.HTTPError as errh:
            # Handle HTTP errors
            #frappe.msgprint(f"HTTP Error: {errh}")
            frappe.msgprint(f"Response Content: {errh.response.content}")
        except requests.exceptions.ConnectionError as errc:
            # Handle Connection errors
            frappe.msgprint(f"Error Connecting: {errc}")
        except requests.exceptions.Timeout as errt:
            # Handle Timeout errors
            frappe.msgprint(f"Timeout Error: {errt}")
        except requests.exceptions.RequestException as err:
            # Handle other exceptions
            frappe.msgprint(f"Request Exception: {err}")
