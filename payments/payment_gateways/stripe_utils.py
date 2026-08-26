# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Shared helpers for the Stripe integration. Everything that more than one
# Stripe module needs (the client, amount conversion, idempotency keys, the
# settings-resolution chain) lives here so the charge, subscription, sync and
# webhook code all agree on the same rules.

import hashlib

import frappe
import stripe
from frappe.utils import flt

# Pin the Stripe API version to the one bundled with stripe~=10.12 so behaviour
# does not drift if the account's default version is bumped in the dashboard.
STRIPE_API_VERSION = "2024-06-20"

# Stripe takes amounts in the smallest currency unit (cents), except
# zero-decimal currencies which are already whole units.
# https://docs.stripe.com/currencies#zero-decimal
ZERO_DECIMAL_CURRENCIES = {
	"BIF",
	"CLP",
	"DJF",
	"GNF",
	"JPY",
	"KMF",
	"KRW",
	"MGA",
	"PYG",
	"RWF",
	"UGX",
	"VND",
	"VUV",
	"XAF",
	"XOF",
	"XPF",
}


def get_stripe_client(stripe_settings):
	"""Return a Stripe client bound to this Stripe Settings doc.

	stripe_settings may be a doc or the docname of a "Stripe Settings" record.

	A fresh stripe.StripeClient is built per call, carrying its own api key,
	pinned api version and http client. This keeps the credentials isolated to
	the caller: mutating module-level stripe.api_key / stripe.api_version races
	across threads, so two concurrent requests for different Stripe accounts
	could charge the wrong account. Route every Stripe call through this client.
	"""
	import stripe

	if isinstance(stripe_settings, str):
		stripe_settings = frappe.get_doc("Stripe Settings", stripe_settings)

	return stripe.StripeClient(
		stripe_settings.get_password("secret_key", raise_exception=False),
		stripe_version=STRIPE_API_VERSION,
		http_client=stripe.http_client.RequestsClient(),
	)


def to_minor_units(amount, currency):
	"""Convert a human amount to the integer Stripe expects (e.g. 12.50 USD -> 1250)."""
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return int(round(flt(amount)))
	return int(round(flt(amount) * 100))


def from_minor_units(amount, currency):
	"""Inverse of to_minor_units (e.g. 1250 USD -> 12.50)."""
	if (currency or "").upper() in ZERO_DECIMAL_CURRENCIES:
		return flt(amount)
	return flt(amount) / 100.0


def idempotency_key(*parts):
	"""Deterministic Stripe idempotency key derived from ERPNext identifiers.

	Same inputs -> same key, so a retried create collapses to one Stripe object
	(Stripe dedupes idempotency keys for 24h). Keep the inputs stable per logical
	operation (e.g. the reference docname + amount), never a timestamp/random.
	"""
	raw = ":".join(str(p) for p in parts if p is not None)
	return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_or_create_customer(client, customer=None, email=None, name=None):
	"""Return a Stripe customer id, reusing Customer.stripe_customer_id when possible.

	`customer` is the ERPNext Customer name (optional). When given, the resolved
	Stripe id is cached back onto Customer.stripe_customer_id so the same Stripe
	customer is reused for every future charge / subscription of that party.
	"""
	has_field = bool(customer) and frappe.db.has_column("Customer", "stripe_customer_id")

	# Reuse the id cached on the Customer.
	if has_field:
		existing = frappe.db.get_value("Customer", customer, "stripe_customer_id")
		if existing:
			try:
				obj = client.customers.retrieve(existing)
				if not obj.get("deleted"):
					return existing
			except stripe.error.InvalidRequestError:
				# Cached id is stale / deleted at Stripe; log a trace and fall through to recreate.
				frappe.log_error(
					f"Stale Stripe customer id {existing} for {customer}", "Stripe customer resolution"
				)

	# Recover a Stripe customer by metadata to dedupe a prior rolled-back attempt.
	if customer:
		found = _find_stripe_customer_by_party(client, customer)
		if found:
			if has_field:
				frappe.db.set_value("Customer", customer, "stripe_customer_id", found, update_modified=False)
			return found

	# Create; name/email can differ per call so no idempotency key (metadata dedupes).
	obj = client.customers.create(
		{
			"email": email,
			"name": name or customer,
			"metadata": {"erpnext_customer": customer} if customer else {},
		}
	)
	if has_field:
		frappe.db.set_value("Customer", customer, "stripe_customer_id", obj.id, update_modified=False)
	return obj.id


def _find_stripe_customer_by_party(client, customer):
	"""Find a non-deleted Stripe customer previously created for this ERPNext party."""
	# Escape quotes so an apostrophe in the party name can't break the search query.
	safe_customer = customer.replace("\\", "\\\\").replace("'", "\\'")
	try:
		result = client.customers.search(
			{"query": f"metadata['erpnext_customer']:'{safe_customer}'", "limit": 1}
		)
	except stripe.error.StripeError:
		return None  # search unavailable / eventual-consistency miss; other errors surface
	for obj in result.get("data") or []:
		if not obj.get("deleted"):
			return obj.id
	return None


def get_stripe_settings_for_gateway(payment_gateway_account):
	"""Resolve a Payment Gateway Account name to its Stripe Settings doc.

	Walks the same chain as get_gateway_controller() in stripe_settings.py:
	    Payment Gateway Account -> Payment Gateway -> Stripe Settings
	Returns None when the account is not backed by Stripe Settings.
	"""
	payment_gateway = frappe.db.get_value(
		"Payment Gateway Account", payment_gateway_account, "payment_gateway"
	)
	if not payment_gateway:
		return None
	gateway = frappe.db.get_value(
		"Payment Gateway", payment_gateway, ["gateway_settings", "gateway_controller"], as_dict=True
	)
	if not gateway or gateway.gateway_settings != "Stripe Settings":
		return None
	if not gateway.gateway_controller:
		# No explicit controller: use the sole Stripe Settings record, only if unambiguous.
		if frappe.db.count("Stripe Settings") != 1:
			return None
		name = frappe.get_all("Stripe Settings", pluck="name", limit=1)[0]
		return frappe.get_doc("Stripe Settings", name)
	return frappe.get_doc("Stripe Settings", gateway.gateway_controller)
