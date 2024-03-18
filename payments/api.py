import frappe
from frappe.utils import nowdate
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry


@frappe.whitelist(allow_guest=True)
def accept_payment(**data):
    """
    headers: X-CALLBACK-TOKEN
    data: {
        "id": "65f675eec32d920fd76d8034",
        "amount": 500000,
        "status": "PAID",
        "created": "2024-03-17T04:47:43.048Z",
        "is_high": false,
        "paid_at": "2024-03-17T04:48:32.000Z",
        "updated": "2024-03-17T04:48:33.041Z",
        "user_id": "65e0a60d213e0478ced4bb3e",
        "currency": "IDR",
        "bank_code": "MANDIRI",
        "payment_id": "46913fa1-3351-47c2-aa55-3b0fde81ee36",
        "description": "Invoice Demo #123",
        "external_id": "invoice-1231241",
        "paid_amount": 500000,
        "payer_email": "ramdani@sopwer.net",
        "merchant_name": "Sopwer",
        "payment_method": "BANK_TRANSFER",
        "payment_channel": "MANDIRI",
        "payment_destination": "8860827838227"
    }
    """

    data = frappe.parse_json(data)
    payment_log = frappe.get_list("Xendit Payment Log", filters={"document": data['external_id']}, fields=["name"])
    if payment_log:
        xpl = frappe.get_doc("Xendit Payment Log", payment_log[0].name)
        token_verify = frappe.db.get_value("Xendit Settings", xpl.xendit_account, "token_verify")
        if frappe.request.headers.get('X-Callback-Token') == token_verify:
            pr = frappe.get_doc(xpl.doc_type, xpl.document)
            pe = get_payment_entry(pr.reference_doctype, pr.reference_name)
            # Ubah status payment entry menjadi "paid"
            pe.reference_no = data['external_id']
            pe.reference_date = data['paid_at'][:10]
            pe.insert(ignore_permissions=True)
            pe.submit()

            # Update Xendit Payment Log
            frappe.db.set_value("Xendit Payment Log", payment_log[0].name, "status", data['status'])
            frappe.db.set_value("Xendit Payment Log", payment_log[0].name, "callback_payload", frappe.as_json(data))
            frappe.db.commit()

            return "Payment entry updated successfully"
        else:
            frappe.log_error("Request Payment {0} Is Invalid".format(data['id']))
            return "Request Payment {0} Is Invalid".format(data['id'])

    else:
        frappe.log_error("Error Payment {0} Log Not Found".format(data['id']))
        return "Payment log not found"
