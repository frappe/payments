# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# Keeps a Subscription Plan's product_price_id in sync with its ERPNext cost.

import frappe
from frappe import _
from frappe.utils import cint

from payments.payment_gateways.stripe_utils import (
	get_stripe_settings_for_gateway,
	to_minor_units,
)

INTERVAL_MAP = {"Day": "day", "Week": "week", "Month": "month", "Year": "year"}


def sync_stripe_price(doc, method=None):
	"""doc_event on Subscription Plan (erpnext) — owned by the payments app."""
	if doc.price_determination not in ("Fixed Rate", "Monthly Rate", "Based On Price List"):
		return
	if not doc.payment_gateway:
		return

	settings = get_stripe_settings_for_gateway(doc.payment_gateway)
	if not settings:
		return  # plan is not on a Stripe gateway

	if not settings.get("sync_subscription_price"):
		return  # opt-in disabled on this Stripe account — keep the manual flow

	# Resolve per-unit recurring amount for Stripe
	unit_cost = _plan_unit_cost(doc)
	if not unit_cost:
		if doc.price_determination == "Based On Price List":
			frappe.msgprint(
				_(
					"No rate found in the plan's Price List for its Item, so the "
					"Stripe price could not be synced."
				),
				indicator="orange",
				alert=True,
			)
		return

	from payments.payment_gateways.stripe_utils import get_stripe_client

	client = get_stripe_client(settings)

	if doc.billing_interval not in INTERVAL_MAP:
		return  # no Stripe-supported recurring interval set yet — nothing to sync

	unit_amount = to_minor_units(unit_cost, doc.currency)
	recurring = {
		"interval": INTERVAL_MAP[doc.billing_interval],
		"interval_count": cint(doc.billing_interval_count) or 1,
	}

	try:
		product_id = None
		old_price_id = None
		if doc.product_price_id:
			existing = client.prices.retrieve(doc.product_price_id)
			if _matches(existing, unit_amount, doc.currency, recurring):
				return  # already in sync — no API write
			product_id = existing.product  # reuse same Product
			old_price_id = doc.product_price_id  # archive only after the new price is live

		if not product_id:
			product_id = client.products.create(
				{"name": doc.plan_name, "metadata": {"erpnext_plan": doc.name}}
			).id

		price = client.prices.create(
			{
				"product": product_id,
				"unit_amount": unit_amount,
				"currency": (doc.currency or "").lower(),
				"recurring": recurring,
				"metadata": {"erpnext_plan": doc.name},
			}
		)
		# We run on_update (after the row is written), so persist directly.
		doc.db_set("product_price_id", price.id, update_modified=False)

		if old_price_id:
			# New price is persisted; safe to archive the old one (prices are immutable).
			client.prices.update(old_price_id, {"active": False})

	except Exception:
		frappe.log_error(frappe.get_traceback(), "Stripe price sync failed")
		frappe.msgprint(
			_("Could not sync this plan's price to Stripe; the Product Price ID may be stale. Please retry."),
			indicator="orange",
			alert=True,
		)


def _matches(price, unit_amount, currency, recurring):
	return bool(
		price.get("active")
		and price.unit_amount == unit_amount
		and (price.currency or "").upper() == (currency or "").upper()
		and price.get("recurring")
		and price.recurring.interval == recurring["interval"]
		and price.recurring.interval_count == recurring["interval_count"]
	)


def _plan_unit_cost(doc):
	"""Per-unit recurring amount in the plan's currency (qty=1).

	Stripe stores a single unit price and multiplies by quantity at checkout,
	so we always push the qty=1 rate.
	"""
	if doc.price_determination == "Based On Price List":
		from payments.utils import erpnext_app_import_guard

		with erpnext_app_import_guard():
			from erpnext.accounts.doctype.subscription_plan.subscription_plan import get_plan_rate

		return get_plan_rate(doc.name, quantity=1)
	# Fixed Rate / Monthly Rate carry a per-interval cost on the plan itself.
	return doc.cost
