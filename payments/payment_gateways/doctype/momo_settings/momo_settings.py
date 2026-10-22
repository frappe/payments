import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url
from urllib.parse import urlencode

from payments.payment_gateways.doctype.momo_settings.momo_connector import MomoConnector

class MoMoSettings(Document):
    def validate_transaction_currency(self, currency):
        supported = self.supported_currencies or "UGX,GHS,XAF,ZMW,RWF"
        if currency not in supported.split(','):
            frappe.throw(_("Currency {0} not supported by MoMo").format(currency))

    def on_update(self):
        """Syncs the Gateway and Mode of Payment on save."""
        from payments.utils import create_payment_gateway
        create_payment_gateway(
            "MoMo-" + self.gateway_name,
            settings="MoMo Settings",
            controller=self.gateway_name,
        )
        create_mode_of_payment("MoMo-" + self.gateway_name, payment_type="Phone")
        callback = (
            frappe.utils.get_url()
            + "/api/method/payments.payment_gateways.doctype.momo_settings"
              ".momo_settings.verify_transaction"
        )
        self.db_set("callback_url", callback, update_modified=False)
        frappe.db.commit()

    def _get_connector(self):
        return MomoConnector(
            env="sandbox" if self.use_sandbox else "production",
            api_user_id=self.api_user_id,
            api_key=self.get_password("api_key"),
            subscription_key=self.get_password("subscription_key"),
            target_environment=self.target_environment
        )

    def get_payment_url(self, **kwargs):
        """Used for Webshop/Checkout redirects."""
        kwargs.setdefault("payment_request_name", kwargs.get("order_id", ""))
        return get_url(f"momo_checkout?{urlencode(kwargs)}")

    @frappe.whitelist(allow_guest=True)
    def request_for_payment(self, **kwargs):
        """Unified entry point for POS, Webshop, and Desk."""
        args = frappe._dict(kwargs)
        
        # 1. IDENTIFY THE SENDER (Handles multiple possible keys from POS/Desk)
        raw_sender = args.sender or args.payer_msisdn or ""
        
        # 2. THE POS "WALKIN" GATEKEEPER
        # Blocks the request if 'WalkIn' is passed instead of a number
        clean_digits = "".join(filter(str.isdigit, str(raw_sender)))
        
        if not clean_digits or str(raw_sender).lower() in ["walkin", "walk-in"]:
            frappe.throw(
                _("MoMo requires a valid phone number. Please enter a phone number in the payment field instead of '{0}'.").format(raw_sender),
                title=_("Invalid Phone Number")
            )

        # 3. AUTO-FORMAT MSISDN (Standardizing to 237...)
        sender = clean_digits
        if sender.startswith("0"):
            sender = "237" + sender[1:]
        elif len(sender) == 9:
            sender = "237" + sender

        # 4. CLEAN THE AMOUNT (MTN XAF often rejects decimals like 50.0)
        try:
            # Converts "50.0" -> 50
            request_amount = str(int(float(args.request_amount or args.amount or 0)))
        except (ValueError, TypeError):
            request_amount = args.request_amount or args.amount

        try:
            connector = self._get_connector()
            callback_url = get_url("/api/method/payments.payment_gateways.doctype.momo_settings.momo_settings.verify_transaction")
            
            response = connector.request_to_pay(
                amount=request_amount,
                currency=args.currency,
                payer_msisdn=sender,
                external_id=args.order_id or args.payment_request_name,
                callback_url=callback_url,
            )

            if response and response.get("referenceId"):
                # Logs the attempt in 'Integration Request'
                create_request_log(args, "Host", "MoMo", response["referenceId"])
            
            return response
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MoMo Request Error")
            frappe.throw(_("MoMo Service Unavailable. Check API credentials."), title=_("Payment Error"))

# ──────────────────────────────────────────────────────────────────────────────
# GLOBAL WHITELISTED METHODS
# ──────────────────────────────────────────────────────────────────────────────

@frappe.whitelist()
def finalize_webshop_order(token, reference_id):
    """
    Called after polling confirms MTN payment is Completed.
    Flow: SO (To Deliver) -> PR (Paid) -> PE (against SO)
    SI is created from SO by warehouse staff after delivery.
    """
    import json

    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        frappe.log_error(
            f"Token: {token}\nRef: {reference_id}",
            "MoMo Finalize: Token Missing"
        )
        return {"error": "Session expired. Contact support with ref: " + reference_id}

    cart_data = json.loads(raw)  # plain dict — NOT frappe._dict
    pr_name     = cart_data.get("payment_request")
    paid_amount = float(cart_data.get("grand_total") or 0)
    quotation   = cart_data.get("quotation_name")

    # DUPLICATE GUARD: only skip if SO already has a submitted PE
    existing_so = frappe.db.get_value(
        "Sales Order",
        {"po_no": quotation, "docstatus": 1},
        "name"
    )
    if existing_so:
        has_pe = frappe.db.exists("Payment Entry Reference", {
            "reference_doctype": "Sales Order",
            "reference_name": existing_so,
        })
        if has_pe:
            frappe.cache().delete_value(f"momo_pending_{token}")
            return {"sales_order": existing_so}

    original_user = frappe.session.user
    try:
        frappe.set_user("Administrator")

        # 1. SALES ORDER
        if not existing_so:
            so = frappe.new_doc("Sales Order")
            so.customer         = cart_data["customer"]
            so.company          = cart_data["company"]
            so.currency         = cart_data["currency"]
            so.delivery_date    = frappe.utils.nowdate()
            so.transaction_date = frappe.utils.nowdate()
            so.order_type       = "Sales"
            so.po_no            = quotation

            if cart_data.get("shipping_address_name"):
                so.shipping_address_name = cart_data["shipping_address_name"]
            if cart_data.get("customer_address"):
                so.customer_address = cart_data["customer_address"]

            for item in cart_data.get("items") or []:
                so.append("items", {
                    "item_code": item["item_code"],
                    "qty":       item.get("qty", 1),
                    "rate":      item.get("rate", 0),
                    "warehouse": item.get("warehouse"),
                    "uom":       item.get("uom"),
                })
            for tax in cart_data.get("taxes") or []:
                so.append("taxes", {
                    "charge_type":  tax.get("charge_type", "On Net Total"),
                    "account_head": tax["account_head"],
                    "description":  tax.get("description") or tax["account_head"],
                    "rate":         tax.get("rate", 0),
                })

            so.flags.ignore_permissions = True
            so.insert()
            so.submit()
            frappe.db.commit()
        else:
            so = frappe.get_doc("Sales Order", existing_so)

        frappe.log_error(f"SO: {so.name}", "MoMo Finalize: Step 1 OK")

        # 2. PAYMENT REQUEST: mark Paid, link to SO
        if pr_name and frappe.db.exists("Payment Request", pr_name):
            frappe.db.set_value("Payment Request", pr_name, {
                "reference_doctype": "Sales Order",
                "reference_name":    so.name,
                "status":            "Paid",
            })
            frappe.db.commit()

        frappe.log_error(f"PR: {pr_name}", "MoMo Finalize: Step 2 OK")

        # 3. PAYMENT ENTRY against SO
        gw_full = (
            frappe.db.get_value("Payment Request", pr_name, "payment_gateway")
            if pr_name else ""
        )
        pga = frappe.db.get_value(
            "Payment Gateway Account",
            {"payment_gateway": gw_full},
            ["payment_account", "currency"],
            as_dict=True,
        ) if gw_full else None

        receivable_account = (
            frappe.db.get_value("Party Account", {
                "parenttype": "Customer",
                "parent":     so.customer,
                "company":    so.company,
            }, "account")
            or frappe.db.get_value("Account", {
                "account_type": "Receivable",
                "company":      so.company,
                "is_group":     0,
            }, "name")
            or frappe.db.get_value("Company", so.company, "default_receivable_account")
        )

        if not pga or not pga.payment_account:
            frappe.throw(f"Payment Gateway Account not configured for {gw_full}")

        pe = frappe.new_doc("Payment Entry")
        pe.payment_type               = "Receive"
        pe.posting_date               = frappe.utils.nowdate()
        pe.company                    = so.company
        pe.party_type                 = "Customer"
        pe.party                      = so.customer
        pe.party_name                 = so.customer_name
        pe.paid_from                  = receivable_account
        pe.paid_from_account_currency = so.currency
        pe.paid_to                    = pga.payment_account
        pe.paid_to_account_currency   = so.currency
        pe.paid_amount                = paid_amount
        pe.received_amount            = paid_amount
        pe.source_exchange_rate       = 1
        pe.target_exchange_rate       = 1
        pe.reference_no               = reference_id
        pe.reference_date             = frappe.utils.nowdate()
        pe.remarks = (
            f"MoMo payment received.\n"
            f"PR: {pr_name} | SO: {so.name} | MTN Ref: {reference_id}"
        )
        pe.append("references", {
            "reference_doctype":  "Sales Order",
            "reference_name":     so.name,
            "allocated_amount":   paid_amount,
            "total_amount":       so.grand_total,
            "outstanding_amount": so.grand_total,
        })
        pe.flags.ignore_permissions = True
        pe.insert()
        pe.submit()
        frappe.db.commit()

        frappe.log_error(f"PE: {pe.name}", "MoMo Finalize: Step 3 OK")

        # 4. SUBMIT QUOTATION so the cart is cleared and cannot be reused
        try:
            quot = frappe.get_doc("Quotation", quotation)
            if quot.docstatus == 0:
                quot.flags.ignore_permissions = True
                quot.submit()
                frappe.db.commit()
            frappe.log_error(f"Quotation {quotation} submitted", "MoMo Finalize: Step 4 OK")
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MoMo Finalize: quotation submit (non-critical)")

        frappe.cache().delete_value(f"momo_pending_{token}")
        frappe.log_error(f"SO: {so.name} | PE: {pe.name}", "MoMo Finalize: COMPLETE")

        return {"sales_order": so.name, "payment_entry": pe.name}

    except Exception:
        frappe.log_error(frappe.get_traceback(), "MoMo Webshop Finalize Error")
        return {"error": "Order creation failed. Contact support with ref: " + reference_id}
    finally:
        frappe.set_user(original_user)


@frappe.whitelist()
def generate_payment_url(payment_request_name):
    """Generates the URL for the 'Request MoMo Payment' button on Invoices."""
    if not payment_request_name:
        frappe.throw(_("Payment Request name is required"))

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
def verify_transaction(**kwargs):
    """Unified Webhook Handler for all MTN MoMo Callbacks."""
    data = frappe._dict(kwargs)
    ref_id = data.get("referenceId") or data.get("externalId")

    if not ref_id or not frappe.db.exists("Integration Request", ref_id):
        return {"status": "error", "message": "Reference not found"}

    integration_request = frappe.get_doc("Integration Request", ref_id)

    if data.get("status") == "SUCCESSFUL":
        original_user = frappe.session.user
        try:
            frappe.set_user("Administrator")
            integration_request.handle_success(data)
            integration_request.db_set("status", "Completed")

            momo_id = data.get("financialTransactionId") or "Confirmed"

            if integration_request.reference_doctype == "Sales Invoice":
                frappe.db.set_value("Sales Invoice", integration_request.reference_docname, {
                    "momo_transaction_verified": 1,
                    "momo_transaction_id": momo_id
                })
            
            if integration_request.reference_doctype == "Payment Request":
                pr = frappe.get_doc("Payment Request", integration_request.reference_docname)
                if pr.status != "Paid":
                    pr.create_payment_entry()
                finalize_webshop_payment(pr.name)
            
            frappe.db.commit()
        except Exception:
            frappe.log_error(frappe.get_traceback(), "MoMo Callback Processing Error")
        finally:
            frappe.set_user(original_user)
    else:
        integration_request.handle_failure(data)
    
    return {"status": "processed"}

@frappe.whitelist(allow_guest=True)
def request_for_payment_by_gateway(gateway_name, **kwargs):
    return frappe.get_doc("MoMo Settings", gateway_name).request_for_payment(**kwargs)

def create_mode_of_payment(gateway, payment_type="General"):
    if not frappe.db.exists("Mode of Payment", gateway):
        mo_pay = frappe.get_doc({
            "doctype": "Mode of Payment",
            "mode_of_payment": gateway,
            "enabled": 1,
            "type": payment_type
        })
        mo_pay.insert(ignore_permissions=True)

@frappe.whitelist(allow_guest=True)
def get_integration_request_status(reference_id):
    if not frappe.db.exists("Integration Request", reference_id):
        return {"status": "Not Found"}
    status = frappe.db.get_value("Integration Request", reference_id, "status")
    return {"status": status}

@frappe.whitelist()
def poll_transaction_status(reference_id, gateway_name=None):
    if not gateway_name:
        gateway_name = frappe.db.get_value("MoMo Settings", {}, "name")
        
    if not gateway_name:
        frappe.throw(_("Please provide a MoMo Settings Gateway Name."))

    if reference_id == "00000000-0000-0000-0000-000000000000":
        try:
            doc = frappe.get_doc("MoMo Settings", gateway_name)
            connector = doc._get_connector() 
            return {"status": "AUTH_SUCCESS", "message": "Credentials verified successfully."}
        except Exception as e:
            frappe.log_error(frappe.get_traceback(), "MoMo Auth Test Failed")
            frappe.throw(_("Authentication Failed: {0}").format(str(e)))

    doc = frappe.get_doc("MoMo Settings", gateway_name)
    connector = doc._get_connector()
    return connector.get_transaction_status(reference_id)
    
@frappe.whitelist()
def poll_pending_transactions(gateway_name=None):
    pending = frappe.get_all("Integration Request", 
        filters={"status": "Queued", "integration_request_service": "MoMo"},
        fields=["name"]
    )
    for entry in pending:
        poll_transaction_status(reference_id=entry.name, gateway_name=gateway_name)
    return True

@frappe.whitelist()
def initiate_webshop_payment(token, gateway_name, phone):
    """
    Called from the MoMo checkout page.
    Reads cart data from cache, creates a Payment Request, sends STK push.
    Does NOT create SO/SI yet — that happens only after payment confirmed.
    """
    import json
    raw = frappe.cache().get_value(f"momo_pending_{token}")
    if not raw:
        return {"error": "Payment session expired. Please return to your cart."}

    cart_data = frappe._dict(json.loads(raw))

    # Build full gateway name
    gw_full = "MoMo-" + gateway_name if not gateway_name.startswith("MoMo-") else gateway_name

    pga = frappe.db.get_value(
        "Payment Gateway Account",
        {"payment_gateway": gw_full},
        ["name", "payment_account", "currency"],
        as_dict=True,
    )

    # Create a lightweight Payment Request (no SO yet — reference is the token)
    pr = frappe.new_doc("Payment Request")
    pr.payment_request_type = "Inward"
    pr.party_type = "Customer"
    pr.party = cart_data.customer
    pr.reference_doctype = "Quotation"
    pr.reference_name = cart_data.quotation_name
    pr.payment_gateway_account = pga.name if pga else ""
    pr.payment_gateway = gw_full
    pr.payment_account = pga.payment_account if pga else ""
    pr.currency = cart_data.currency
    pr.grand_total = cart_data.grand_total
    pr.base_grand_total = cart_data.grand_total
    pr.outstanding_amount = cart_data.grand_total
    pr.email_to = cart_data.contact_email or cart_data.customer
    pr.subject = f"Webshop payment for {cart_data.customer_name}"
    pr.flags.ignore_permissions = True
    pr.insert()
    frappe.db.set_value("Payment Request", pr.name, {"docstatus": 1, "status": "Requested"})

    # Store PR name back into the token cache so finalize can find it
    cart_data["payment_request"] = pr.name
    frappe.cache().set_value(f"momo_pending_{token}", json.dumps(cart_data), expires_in_sec=1800)

    # Clean and format phone
    clean_phone = "".join(filter(str.isdigit, str(phone)))
    if len(clean_phone) == 9:
        clean_phone = "237" + clean_phone

    # Get connector and send STK push
    settings_name = frappe.db.get_value("MoMo Settings", {"gateway_name": gateway_name}, "name")
    if not settings_name:
        return {"error": f"MoMo Settings not found for: {gateway_name}"}

    momo_doc = frappe.get_doc("MoMo Settings", settings_name)
    connector = momo_doc._get_connector()

    int_amount = str(int(float(cart_data.grand_total)))
    callback_url = frappe.utils.get_url(
        "/api/method/payments.payment_gateways.doctype.momo_settings.momo_settings.verify_transaction"
    )

    response = connector.request_to_pay(
        amount=int_amount,
        currency=cart_data.currency,
        payer_msisdn=clean_phone,
        external_id=pr.name,
        callback_url=callback_url,
    )

    if not response or not response.get("referenceId"):
        return {"error": "MTN did not return a reference ID. Check phone number and try again."}

    reference_id = response["referenceId"]

    from frappe.integrations.utils import create_request_log
    args = frappe._dict(
        sender=clean_phone, request_amount=int_amount,
        currency=cart_data.currency, order_id=pr.name,
        reference_doctype="Payment Request", reference_docname=pr.name,
    )
    create_request_log(args, "Host", "MoMo", reference_id)
    frappe.db.set_value("Integration Request", reference_id, {
        "reference_doctype": "Payment Request",
        "reference_docname": pr.name,
    })
    frappe.db.commit()

    return {"reference_id": reference_id, "payment_request": pr.name}


@frappe.whitelist()
@frappe.whitelist()
def poll_webshop_transaction(reference_id, gateway_name=None):
    """
    Active poll: checks DB first, then asks MTN directly if still pending.
    Returns {"status": "SUCCESSFUL" | "FAILED" | "PENDING" | "TIMEOUT"}
    Safe to call repeatedly — never creates Payment Entries itself.
    """
    if not frappe.db.exists("Integration Request", reference_id):
        return {"status": "PENDING"}  # not logged yet, still in flight

    db_status = frappe.db.get_value("Integration Request", reference_id, "status")

    # Fast path — already resolved in DB
    if db_status == "Completed":
        return {"status": "SUCCESSFUL"}
    if db_status in ("Failed", "Cancelled"):
        return {"status": "FAILED"}

    # Still Queued/Pending — ask MTN directly
    try:
        # Resolve gateway name
        bare_name = (gateway_name or "").replace("MoMo-", "") if gateway_name else None
        if not bare_name:
            # Try to find it from the Integration Request -> Payment Request -> gateway
            ir = frappe.get_doc("Integration Request", reference_id)
            pr_name = ir.reference_docname
            if pr_name and frappe.db.exists("Payment Request", pr_name):
                gw_full = frappe.db.get_value("Payment Request", pr_name, "payment_gateway") or ""
                bare_name = gw_full.replace("MoMo-", "")

        if not bare_name:
            return {"status": "PENDING"}

        settings_name = frappe.db.get_value("MoMo Settings", {"gateway_name": bare_name}, "name")
        if not settings_name:
            return {"status": "PENDING"}

        momo_doc = frappe.get_doc("MoMo Settings", settings_name)
        connector = momo_doc._get_connector()
        mtn_resp  = connector.get_transaction_status(reference_id)
        mtn_status = (mtn_resp or {}).get("status", "PENDING")

        if mtn_status == "SUCCESSFUL":
            # Update DB so callback isn't needed
            frappe.db.set_value("Integration Request", reference_id, "status", "Completed")
            frappe.db.commit()
            return {"status": "SUCCESSFUL"}

        if mtn_status == "FAILED":
            frappe.db.set_value("Integration Request", reference_id, "status", "Failed")
            frappe.db.commit()
            return {"status": "FAILED"}

        return {"status": "PENDING"}

    except Exception:
        frappe.log_error(frappe.get_traceback(), "MoMo poll_webshop_transaction Error")
        return {"status": "PENDING"}  # don't surface errors to frontend, just keep polling


def on_sales_invoice_submit(doc, method=None):
    """
    Triggered when a Sales Invoice is submitted.
    If the SI was created from a Sales Order that has a MoMo Payment Entry,
    automatically relinks the PE from the SO to the SI and marks SI as Paid.
    """
    # Find all SOs linked to this SI
    so_names = list(set(
        row.sales_order
        for row in doc.get("items") or []
        if row.get("sales_order")
    ))
    if not so_names:
        return

    for so_name in so_names:
        # Find a submitted PE referencing this SO
        pe_ref = frappe.db.get_value(
            "Payment Entry Reference",
            {
                "reference_doctype": "Sales Order",
                "reference_name":    so_name,
            },
            ["parent", "allocated_amount"],
            as_dict=True,
        )
        if not pe_ref:
            continue

        pe_name      = pe_ref.parent
        paid_amount  = float(pe_ref.allocated_amount or 0)

        # Confirm PE is submitted
        pe_status = frappe.db.get_value("Payment Entry", pe_name, "docstatus")
        if pe_status != 1:
            continue

        try:
            # Remove old SO reference row directly in DB
            frappe.db.delete("Payment Entry Reference", {
                "parent":               pe_name,
                "reference_doctype":    "Sales Order",
                "reference_name":       so_name,
            })

            # Insert SI reference row directly
            frappe.db.sql("""
                INSERT INTO `tabPayment Entry Reference`
                    (name, parent, parenttype, parentfield,
                     reference_doctype, reference_name,
                     allocated_amount, total_amount, outstanding_amount)
                VALUES (%s, %s, 'Payment Entry', 'references',
                        'Sales Invoice', %s, %s, %s, %s)
            """, (
                frappe.generate_hash(length=10),
                pe_name,
                doc.name,
                paid_amount,
                doc.grand_total,
                doc.grand_total,
            ))

            # Force SI to Paid
            outstanding = max(0.0, float(doc.grand_total) - paid_amount)
            status = "Paid" if outstanding == 0 else "Partly Paid"
            frappe.db.set_value("Sales Invoice", doc.name, {
                "outstanding_amount": outstanding,
                "status":             status,
            })

            frappe.db.commit()
            frappe.log_error(
                f"PE {pe_name} relinked from SO {so_name} to SI {doc.name}",
                "MoMo: SI Auto-Reconcile OK"
            )

        except Exception:
            frappe.log_error(
                frappe.get_traceback(),
                f"MoMo: SI Auto-Reconcile Failed — PE {pe_name} / SI {doc.name}"
            )
