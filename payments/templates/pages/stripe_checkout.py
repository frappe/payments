# Copyright (c) 2021, Frappe Technologies Pvt. Ltd. and Contributors
# License: MIT. See LICENSE
import json

import frappe
from frappe import _
from frappe.utils import cint, fmt_money

from payments.payment_gateways.doctype.stripe_settings.stripe_settings import (
	get_gateway_controller,
)

no_cache = 1

expected_keys = (
	"amount",
	"title",
	"description",
	"reference_doctype",
	"reference_docname",
	"payer_name",
	"payer_email",
	"currency",
	"payment_gateway",
)


def get_context(context):
	context.no_cache = 1

	# all these keys exist in form_dict
	if not (set(expected_keys) - set(list(frappe.form_dict))):
		for key in expected_keys:
			context[key] = frappe.form_dict[key]
		gateway_controller = get_gateway_controller(
			context.reference_doctype, context.reference_docname, context.payment_gateway
		)
		context.publishable_key = get_api_key(context.reference_docname, gateway_controller)
		context.image = get_header_image(context.reference_docname, gateway_controller)

		context["amount"] = fmt_money(amount=context["amount"], currency=context["currency"])

		if is_a_subscription(context.reference_doctype, context.reference_docname):
			payment_plans = frappe.db.get_all(
				"Subscription Plan Detail",
				filters={"parent": context.reference_docname, "parenttype": context.reference_doctype},
				fields=["plan"],
				limit=1,
			)
			if payment_plans:
				billing_interval, billing_interval_count = frappe.db.get_value(
					"Subscription Plan", payment_plans[0].plan, ["billing_interval", "billing_interval_count"]
				)
				billing_interval_count = cint(billing_interval_count) or 1
				if billing_interval_count == 1:
					recurrence = _("per {0}").format(_(billing_interval))
				else:
					recurrence = _("every {0} {1}s").format(billing_interval_count, _(billing_interval))

				context["amount"] = context["amount"] + " " + recurrence
	else:
		frappe.redirect_to_message(
			_("Some information is missing"),
			_("Looks like someone sent you to an incomplete URL. Please ask them to look into it."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect


def get_api_key(doc, gateway_controller):
	publishable_key = frappe.db.get_value("Stripe Settings", gateway_controller, "publishable_key")
	if cint(frappe.form_dict.get("use_sandbox")):
		publishable_key = frappe.conf.sandbox_publishable_key

	return publishable_key


def get_header_image(doc, gateway_controller):
	return frappe.db.get_value("Stripe Settings", gateway_controller, "header_img")


def guard_payment_reference(reference_doctype, reference_docname):
	"""Guard the guest-exposed payment endpoints against arbitrary references.

	The reference must be a real document. Authenticated callers must also have
	read access; guests legitimately cannot (Payment Request grants no Guest
	permission — the checkout return settles as Administrator for the same
	reason), so for them the existence check is the guard.
	"""
	if not (
		reference_doctype and reference_docname and frappe.db.exists(reference_doctype, reference_docname)
	):
		frappe.throw(_("Invalid payment reference."), frappe.PermissionError)
	if frappe.session.user != "Guest":
		frappe.has_permission(reference_doctype, "read", reference_docname, throw=True)


def get_reference_amount(reference_doctype, reference_docname):
	"""Authoritative payable amount + currency, read server-side from the reference.

	Never trust a client-supplied amount: without this an attacker can post any
	value (e.g. 0.01) and settle a full order for a token amount. Payment
	Requests carry the payable total in `grand_total`.
	"""
	meta = frappe.get_meta(reference_doctype)
	amount_field = "grand_total" if meta.has_field("grand_total") else "amount"
	if not meta.has_field(amount_field):
		frappe.throw(_("Cannot determine the payable amount for {0}.").format(reference_doctype))
	fields = [amount_field] + (["currency"] if meta.has_field("currency") else [])
	row = frappe.db.get_value(reference_doctype, reference_docname, fields, as_dict=True)
	return row.get(amount_field), row.get("currency")


@frappe.whitelist(allow_guest=True)
def create_payment_intent(data, reference_doctype=None, reference_docname=None, payment_gateway=None):
	"""Embedded Elements: create an unconfirmed PaymentIntent, return its client_secret.

	The PaymentElement confirms it client-side (handling 3DS); make_payment then
	verifies it server-side.
	"""
	guard_payment_reference(reference_doctype, reference_docname)
	data = json.loads(data)
	# Reference + amount/currency are authoritative server-side, never the client.
	# Stamping the reference binds the PaymentIntent's metadata to this order so it
	# can't later be replayed to settle a different one.
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	data["amount"], currency = get_reference_amount(reference_doctype, reference_docname)
	if currency:
		data["currency"] = currency
	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)
	settings = frappe.get_doc("Stripe Settings", gateway_controller)
	result = settings.create_payment_intent_for_checkout(frappe._dict(data))
	frappe.db.commit()
	return result


@frappe.whitelist(allow_guest=True)
def make_payment(
	data,
	reference_doctype=None,
	reference_docname=None,
	payment_gateway=None,
	payment_intent=None,
	stripe_token_id=None,
):
	guard_payment_reference(reference_doctype, reference_docname)
	data = json.loads(data)
	# Reference + amount/currency are authoritative server-side, never the client.
	# The reference is what gets settled and is cross-checked against the intent's
	# metadata so a payment for one order can't settle another.
	data["reference_doctype"] = reference_doctype
	data["reference_docname"] = reference_docname
	data["amount"], currency = get_reference_amount(reference_doctype, reference_docname)
	if currency:
		data["currency"] = currency

	if payment_intent:
		data.update({"payment_intent": payment_intent})
	if stripe_token_id:
		# Backward-compat with the pre-PaymentIntents checkout JS.
		data.update({"stripe_token_id": stripe_token_id})

	gateway_controller = get_gateway_controller(reference_doctype, reference_docname, payment_gateway)

	if is_a_subscription(reference_doctype, reference_docname):
		reference = frappe.get_doc(reference_doctype, reference_docname)
		data = reference.create_subscription("stripe", gateway_controller, data)
	else:
		data = frappe.get_doc("Stripe Settings", gateway_controller).create_request(data)

	frappe.db.commit()
	return data


def is_a_subscription(reference_doctype, reference_docname):
	if not frappe.get_meta(reference_doctype).has_field("is_a_subscription"):
		return False
	return frappe.db.get_value(reference_doctype, reference_docname, "is_a_subscription")
