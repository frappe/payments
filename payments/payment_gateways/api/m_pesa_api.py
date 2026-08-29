
from __future__ import unicode_literals
import frappe, requests
from frappe import _
from requests.auth import HTTPBasicAuth
import json


def get_token(app_key, app_secret, base_url):
    authenticate_uri = "/oauth/v1/generate?grant_type=client_credentials"
    authenticate_url = "{0}{1}".format(base_url, authenticate_uri)

    r = requests.get(authenticate_url, auth=HTTPBasicAuth(app_key, app_secret))

    return r.json()["access_token"]


@frappe.whitelist(allow_guest=True)
def confirmation(**kwargs):
    try:
        args = frappe._dict(kwargs)
        doc = frappe.new_doc("Mpesa C2B Payment Register")
        doc.transactiontype = args.get("TransactionType")
        doc.transid = args.get("TransID")
        doc.transtime = args.get("TransTime")
        doc.transamount = args.get("TransAmount")
        doc.businessshortcode = args.get("BusinessShortCode")
        doc.billrefnumber = args.get("BillRefNumber")
        doc.invoicenumber = args.get("InvoiceNumber")
        doc.orgaccountbalance = args.get("OrgAccountBalance")
        doc.thirdpartytransid = args.get("ThirdPartyTransID")
        doc.msisdn = args.get("MSISDN")
        doc.firstname = args.get("FirstName")
        doc.middlename = args.get("MiddleName")
        doc.lastname = args.get("LastName")
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        context = {"ResultCode": 0, "ResultDesc": "Accepted"}
        return dict(context)
    except Exception as e:
        frappe.log_error(frappe.get_traceback(), str(e)[:140])
        context = {"ResultCode": 1, "ResultDesc": "Rejected"}
        return dict(context)


@frappe.whitelist(allow_guest=True)
def validation(**kwargs):
    context = {"ResultCode": 0, "ResultDesc": "Accepted"}
    return dict(context)


@frappe.whitelist()
def get_mpesa_mode_of_payment(company):
    modes = frappe.get_all(
        "Mpesa C2B Payment Register URL",
        filters={"company": company, "register_status": "Success"},
        fields=["mode_of_payment"],
    )
    modes_of_payment = []
    for mode in modes:
        if mode.mode_of_payment not in modes_of_payment:
            modes_of_payment.append(mode.mode_of_payment)
    return modes_of_payment

# @frappe.whitelist(allow_guest=True)
# def get_mpesa_draft_payments(name, msisdn, transid, full_name, mode_of_payment, company):
   
#     payments = frappe.get_all(
#         "Mpesa C2B Payment Register",
#         filters={'docstatus':0},
#         fields=[
#             "name",
#             "company",
#             "msisdn",
#             "full_name",
#             "posting_date",
#             "transamount",
           
#         ],
#         order_by="posting_date desc",
#     )
#     frappe.response['message']=payments
#     return payments
# @frappe.whitelist(allow_guest=True)
@frappe.whitelist(allow_guest=True)
def get_mpesa_draft_c2b_payments(search_term):
    fields = [
        "name",
        "company",
        "msisdn",
        "full_name",
        "posting_date",
        "posting_time",
        "transamount",
    ]

    filters = {"docstatus": 0}
    order_by="posting_date desc, posting_time desc"

    if search_term:
        payments_by_msisdn = frappe.get_all(
            "Mpesa C2B Payment Register",
            filters={"msisdn": ["like", f"%{search_term}%"], "docstatus": 0},
            fields=fields,
            order_by=order_by
        )
        payments_by_full_name = frappe.get_all(
            "Mpesa C2B Payment Register",
            filters={"full_name": ["like", f"%{search_term}%"], "docstatus": 0},
            fields=fields,
            order_by=order_by
        )

        # Merge results from both queries
        payments = payments_by_full_name + payments_by_msisdn
    else:
        # If search_term or status is not provided, return all payments with the given status
        payments = frappe.get_all(
            "Mpesa C2B Payment Register", filters=filters, fields=fields,order_by=order_by
        )
    
    return payments
    
@frappe.whitelist(allow_guest=True)
def get_draft_pos_invoice(search_term=None):
    from frappe.query_builder import DocType
    from frappe.query_builder.functions import Concat
    from frappe import qb

    SalesInvoice = DocType("Sales Invoice")
    fields = ["*"]
    status_filters = ["Overdue", "Partially Paid", "Unpaid", "Overdue and Discounted", "Partially Paid and Discounted"]

    # Create the base query
    query = (
        qb.from_(SalesInvoice)
        .select(*fields)
        .where(SalesInvoice.docstatus == 1)
        .where(SalesInvoice.status.isin(status_filters))
        .orderby(SalesInvoice.posting_date, order=qb.desc)
    )

    if search_term:
        search_filter = (
            (SalesInvoice.customer.like(f"%{search_term}%")) |
            (SalesInvoice.name.like(f"%{search_term}%"))
        )
        query = query.where(search_filter)

    invoices = query.run(as_dict=True)

    frappe.response['message'] = invoices

@frappe.whitelist()
def submit_mpesa_payment(mpesa_payment, customer, mode_of_payment):
    try:
        doc = process_mpesa_payment(mpesa_payment, customer, mode_of_payment, submit_payment=True)
        return frappe.get_doc("Payment Entry", doc.payment_entry)
    except Exception as e:
        frappe.log_error(f"Error: {str(e)}", "submit_mpesa_payment")
        raise

@frappe.whitelist()
def submit_instant_mpesa_payment():
    mpesa_payment = frappe.form_dict.get("mpesa_payment")
    customer = frappe.form_dict.get("customer")
    pos_profile = frappe.form_dict.get("pos_profile")
    mode_of_payment = get_payment_method(pos_profile)

    try:
        process_mpesa_payment(mpesa_payment, customer, mode_of_payment, submit_payment=False)
    except Exception as e:
        frappe.log_error(f"Error: {str(e)}", "submit_instant_mpesa_payment")
        raise

def process_mpesa_payment(mpesa_payment, customer, mode_of_payment, submit_payment=False):
    try:
        doc = frappe.get_doc("Mpesa C2B Payment Register", mpesa_payment)
        doc.customer = customer
        doc.mode_of_payment = mode_of_payment
        #TODO: after testing, mode of payment
        #doc.mode_of_payment = get_mode_of_payment(doc)
        doc.submit_payment=submit_payment
        doc.save()
        doc.submit()
        frappe.db.commit()  

        doc.reload()  

        return doc
    except Exception as e:
        frappe.log_error(f"Error: {str(e)}", "process_mpesa_payment")
        raise

def get_payment_method(pos_profile):
    pos_profile_doc = frappe.get_doc("POS Profile", pos_profile)
    for payment in pos_profile_doc.payments:
        if payment.default == 1:
            return payment.mode_of_payment
    return None

def get_mode_of_payment(mpesa_doc):
    business_short_code=mpesa_doc.businessshortcode
    mode_of_payment = frappe.get_value("Mpesa C2B Payment Register URL", business_short_code, "mode_of_payment")
    return mode_of_payment
    
