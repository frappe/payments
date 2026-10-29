# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _

from  payments.payment_gateways.doctype.phonepe_settings.phonepe_settings import(
        initiate_payment
)

no_cache = 1

def get_context(context):
    context.no_cache = 1
    doc = frappe.get_doc("Integration Request", frappe.form_dict["merchant_transaction_id"])
    merchant_txn_id = doc.name
    data = json.loads(doc.data)  # Parse the data string as a JSON object
    data["payer_name"] = ascii_to_text(data["payer_name"])
    merchant_user_id = create_username(data["payer_name"])
    amount = rupees_to_paise(data["amount"])
    mobile_no = "9000000000"
    payment_url = initiate_payment(merchant_txn_id, merchant_user_id, amount,mobile_no)
    return {
        "payment_url": payment_url
    }

def ascii_to_text(ascii_values):
    return ''.join(chr(i) for i in ascii_values)

def create_username(customer_name):
    username = customer_name.replace(" ", "_").replace("-", "_")
    while "__" in username:
        username = username.replace("__", "_")
    return username

def rupees_to_paise(rupees):
    return int(rupees * 100)
