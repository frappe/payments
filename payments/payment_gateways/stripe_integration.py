# Copyright (c) 2018, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Programmatic subscription creation (V1 create_subscription); Hosted Checkout is primary.

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log

from payments.payment_gateways.stripe_utils import (
	find_erpnext_subscription,
	get_or_create_customer,
	get_stripe_client,
	get_subscription_line_items,
	idempotency_key,
	link_stripe_subscription,
)


def create_stripe_subscription(gateway_controller, data):
	stripe_settings = frappe.get_doc("Stripe Settings", gateway_controller)
	stripe_settings.data = frappe._dict(data)
	stripe_settings.stripe = get_stripe_client(stripe_settings)

	try:
		stripe_settings.integration_request = create_request_log(stripe_settings.data, "Host", "Stripe")
		return create_subscription_on_stripe(stripe_settings)

	except Exception:
		frappe.log_error(frappe.get_traceback(), "Unable to create Stripe subscription")
		return {
			"redirect_to": frappe.redirect_to_message(
				_("Server Error"),
				_(
					"It seems that there is an issue with the server's stripe configuration. In case of failure, the amount will get refunded to your account."
				),
			),
			"status": 401,
		}


def create_subscription_on_stripe(stripe_settings):
	client = stripe_settings.stripe
	data = stripe_settings.data

	pr = frappe.get_doc("Payment Request", data.reference_docname)
	party = pr.party if pr.party_type == "Customer" else None
	items = get_subscription_line_items("Payment Request", pr.name)
	plan_names = [
		row.plan
		for row in frappe.get_all(
			"Subscription Plan Detail",
			filters={"parent": pr.name, "parenttype": "Payment Request"},
			fields=["plan"],
		)
	]

	customer_id = get_or_create_customer(
		client, customer=party, email=data.get("payer_email"), name=data.get("payer_name") or party
	)
	erpnext_sub = find_erpnext_subscription(party, plan_names)
	metadata = {"reference_doctype": "Payment Request", "reference_docname": pr.name}
	if erpnext_sub:
		metadata["erpnext_subscription"] = erpnext_sub
	if party:
		metadata["erpnext_customer"] = party

	if (stripe_settings.subscription_billing_model or "") == "Charge Now + Defer First Cycle":
		frappe.throw(
			_(
				"The 'Charge Now + Defer First Cycle' billing model is not supported yet — "
				"please use 'Bill From Cycle One'."
			)
		)

	try:
		create_args = {
			"customer": customer_id,
			"items": items,
			"metadata": metadata,
			"payment_behavior": "default_incomplete",
			"payment_settings": {"save_default_payment_method": "on_subscription"},
			"expand": ["latest_invoice.payment_intent"],
		}

		subscription = client.subscriptions.create(
			create_args, {"idempotency_key": idempotency_key("sub", pr.name)}
		)
		stripe_settings.integration_request.db_set("output", subscription.id, update_modified=False)
		link_stripe_subscription(erpnext_sub, subscription.id, customer_id)

		if subscription.status in ("active", "trialing"):
			stripe_settings.integration_request.db_set("status", "Completed", update_modified=False)
			stripe_settings.flags.status_changed_to = "Completed"
		elif subscription.status == "incomplete":
			# First invoice needs client-side confirmation (3DS / no default card).
			# Don't mark the Payment Request paid; the invoice.paid webhook will.
			intent = getattr(getattr(subscription, "latest_invoice", None), "payment_intent", None)
			if intent is not None:
				return {
					"requires_action": True,
					"client_secret": intent.client_secret,
					"payment_intent": intent.id,
					"status": "Pending",
				}
			stripe_settings.integration_request.db_set("status", "Pending", update_modified=False)
		else:
			stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
			frappe.log_error(f"Stripe Subscription ID {subscription.id}: status {subscription.status}")

	except Exception:
		stripe_settings.integration_request.db_set("status", "Failed", update_modified=False)
		frappe.log_error(frappe.get_traceback(), "Unable to create Stripe subscription")

	stripe_settings.data.setdefault("reference_doctype", "Payment Request")
	stripe_settings.data.setdefault("reference_docname", pr.name)
	return stripe_settings.finalize_request()
