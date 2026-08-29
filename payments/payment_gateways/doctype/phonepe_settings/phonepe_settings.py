# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

import json
import base64
import hashlib
import requests
import webbrowser
from urllib.parse import urlencode
import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import (
    call_hook_method,
    cint,
    cstr,
    flt,
    get_request_site_address,
    get_url,
)

class PhonePeSettings(Document):
    supported_currencies = ["INR"]

    def validate_transaction_currency(self, currency):
        if currency not in self.supported_currencies:
            frappe.throw(
                _(
                    "Please select another payment method. PhonePe does not support transactions in currency '{0}'"
                ).format(currency)
            )

    def get_payment_url(self, **kwargs):
        """Return payment url with several params"""
        # create unique merchant transaction id by making it equal to the integration request
        integration_request = create_request_log(kwargs, service_name="Phonepe")
        kwargs.update(dict(merchant_transaction_id=integration_request.name))

        return get_url(f"./phonepe_checkout?{urlencode(kwargs)}")

def get_phonepe_config():
    """Get PhonePe configuration."""
    settings = frappe.get_doc("PhonePe Settings", "PhonePe Settings")
    merchant_id = settings.get('merchant_id')
    salt_key = settings.get_password('salt_key')
    index = settings.get('index')
    sandbox = settings.get('sandbox')

    host_url = 'https://api-preprod.phonepe.com/apis/pg-sandbox' if sandbox == 1 else 'https://api.phonepe.com/apis/hermes'

    phonepe_config = {
        'merchantId': merchant_id,
        'saltKey': salt_key,
        'index': index,
        'host_url': host_url,
        'pay_endpoint': '/pg/v1/pay',
        'status_endpoint': '/pg/v1/status/'
    }
    return phonepe_config

def calculate_sha256(input_string: str) -> str:
    """Calculate the SHA-256 hash of an input string."""
    input_bytes = input_string.encode('utf-8')
    sha256_hash = hashlib.sha256(input_bytes)
    return sha256_hash.hexdigest()

def base64_encode_dict(input_dict: dict) -> str:
    """Encode a dictionary as a JSON string and then Base64."""
    json_data = json.dumps(input_dict)
    data_bytes = json_data.encode('utf-8')
    return base64.b64encode(data_bytes).decode('utf-8')

def create_main_payload(phonepe_config, merchant_txn_id, merchant_user_id, amount, mobile_no) -> dict:
    """Create the main payload dictionary for the API request."""
    redirect_url = get_url(f'./api/method/payments.payment_gateways.doctype.phonepe_settings.phonepe_settings.process_redirect')
    #callback_url = get_url(f'./api/method/payments.payment_gateways.doctype.phonepe_settings.phonepe_settings.process_callback')
    return {
        "merchantId": phonepe_config["merchantId"],
        "merchantTransactionId": merchant_txn_id,
        "merchantUserId": merchant_user_id,
        "amount": amount, 
        "redirectUrl": redirect_url,
        "redirectMode": "POST",
        "callbackUrl": redirect_url,
        "mobileNumber": mobile_no,
        "paymentInstrument": {
            "type": "PAY_PAGE"
        }
    }

@frappe.whitelist(allow_guest=True)
def initiate_payment(merchant_txn_id, merchant_user_id, amount, mobile_no):
    """Make a payment request to the PhonePe API."""
    phonepe_config = get_phonepe_config()
    main_payload = create_main_payload(phonepe_config, merchant_txn_id, merchant_user_id, amount, mobile_no)
    index = phonepe_config['index']
    endpoint = phonepe_config['pay_endpoint']
    salt_key = phonepe_config['saltKey']

    base64_string = base64_encode_dict(main_payload)
    main_string = base64_string + endpoint + salt_key
    sha256_val = calculate_sha256(main_string)
    check_sum = sha256_val + '###' + index

    headers = {
        'Content-Type': 'application/json',
        'X-VERIFY': check_sum,
        'accept': 'application/json',
    }
    json_data = {
        'request': base64_string,
    }

    url = phonepe_config['host_url'] + endpoint
    try:
        response = requests.post(url, headers=headers, json=json_data)
        response.raise_for_status()
        response_data = response.json()
        #print(response_data)
        return redirect_to_paymentPage(response_data)
    except requests.exceptions.RequestException as err:
        print("Error:", err)

def redirect_to_paymentPage(response_data):
    # Check if the 'url' key exists in the response data
    if 'data' in response_data and 'instrumentResponse' in response_data['data'] and 'redirectInfo' in response_data['data']['instrumentResponse']:
        if 'url' in response_data['data']['instrumentResponse']['redirectInfo']:
            payment_url = response_data['data']['instrumentResponse']['redirectInfo']['url']
            return payment_url
        else:
            # Handle the error, e.g., by returning a user-friendly error message
            return "Error: Missing 'url' in response", 500
    else:
        print("Invalid response data. Unable to find payment URL.")

@frappe.whitelist(allow_guest=True)
def process_redirect(**phonepe_response):
    # Get the JSON response from the request
    merchant_transaction_id = phonepe_response.pop('transactionId',None)
    if merchant_transaction_id:
        payment_status = check_payment_status(merchant_transaction_id)
        finalize_request(merchant_transaction_id, payment_status)
        #return f'Payment successful {payment_status}'
    else:
        raise Exception("Merchant Transaction Id missing in the request")

def check_payment_status(merchant_transaction_id):
    phonepe_config = get_phonepe_config()
    index = phonepe_config['index']
    salt_key = phonepe_config['saltKey']
    merchant_id = phonepe_config['merchantId']
    url = phonepe_config['host_url']
    status_endpoint = phonepe_config['status_endpoint']

    sha256_Pay_load_String = f'{status_endpoint}{merchant_id}/' + merchant_transaction_id + salt_key;
    sha256_val = calculate_sha256(sha256_Pay_load_String);
    checksum = sha256_val + '###' + index;

    request_url = f'{url}{status_endpoint}{merchant_id}/' + merchant_transaction_id;
    headers = {
        'Content-Type': 'application/json',
        'X-VERIFY': checksum,
        'X-MERCHANT-ID': merchant_transaction_id,
        'accept': 'application/json',
    }
    payment_status_response = requests.get(request_url, headers=headers)
    #finalize_request(payment_status_response.json())
    return payment_status_response.json()

def finalize_request(merchant_transaction_id, payment_status_response):
    request = frappe.get_doc("Integration Request", merchant_transaction_id)
    transaction_data = frappe._dict(json.loads(request.data))
    redirect_to = transaction_data.get("redirect_to") or None
    redirect_message = transaction_data.get("redirect_message") or None

    if payment_status_response["code"] == "PAYMENT_SUCCESS":
        if transaction_data.reference_doctype and transaction_data.reference_docname:
            custom_redirect_to = None
            try:
                custom_redirect_to = frappe.get_doc(
                    transaction_data.reference_doctype, transaction_data.reference_docname
                ).run_method("on_payment_authorized", "Completed")
                request.db_set("status", "Completed")
            except Exception:
                request.db_set("status", "Failed")
                frappe.log_error(frappe.get_traceback())

            if custom_redirect_to:
                redirect_to = custom_redirect_to

            redirect_url = "/payment-success"
    else:
        request.db_set("status", "Failed")
        redirect_url = "/payment-failed"

    if redirect_to:
        redirect_url += "?" + urlencode({"redirect_to": redirect_to})
    if redirect_message:
        redirect_url += "&" + urlencode({"redirect_message": redirect_message})

    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = redirect_url

def get_gateway_controller(doctype, docname):
	reference_doc = frappe.get_doc(doctype, docname)
	gateway_controller = frappe.db.get_value(
		"Payment Gateway", reference_doc.payment_gateway, "gateway_controller"
	)
	return gateway_controller
