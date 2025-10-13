import stripe
import frappe
from frappe import _
from frappe.integrations.utils import create_request_log

def cancel_stripe_subscription(subscription_doc):
    """
    Cancel an active Stripe subscription from ERPNext Subscription Doc
    """
    # Get Stripe Settings
    if subscription_doc.status == "Cancelled":
        stripe_settings = frappe.get_doc("Stripe Settings", "Stripe")
        stripe.api_key = stripe_settings.get_password(fieldname="secret_key", raise_exception=False)
        stripe.default_http_client = stripe.http_client.RequestsClient()

        try:
            # Log the cancellation request
            stripe_settings.integration_request = create_request_log(
                {"subscription_name": subscription_doc.name},
                "Host",
                "Stripe"
            )

            # Fetch the Stripe subscription ID from the ERPNext subscription doc
            stripe_subscription_id = subscription_doc.custom_stripe_subscription_id
            if not stripe_subscription_id:
                frappe.throw(_("No Stripe Subscription ID found for this subscription"))

            # Cancel the subscription in Stripe
            stripe.Subscription.update(stripe_subscription_id,{"cancel_at_period_end" :True})

            # Update Integration Log
            stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
            subscription_doc.db_set("status", "Cancelled")
            frappe.msgprint(_("Stripe subscription cancelled successfully"))

        except Exception as e:
            stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
            frappe.log_error(f"Unable to cancel Stripe subscription: {str(e)}", _("Stripe Subscription Cancel"))

        return True
