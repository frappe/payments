# Copyright (c) 2018, Frappe Technologies and contributors
# For license information, please see license.txt


import hashlib
import hmac
import json

import frappe


@frappe.whitelist(allow_guest=True)
def webhooks():
	r = frappe.request
	if not r:
		return

	if not authenticate_signature(r):
		raise frappe.AuthenticationError

	gocardless_events = json.loads(r.get_data()) or []
	for event in gocardless_events["events"]:
		set_status(event)

	return 200


def set_status(event):
	resource_type = event.get("resource_type", {})
	reference_doctype = event.get("resource_metadata", {}).get('reference_doctype')

	if resource_type == "mandates":
		set_mandate_status(event)
	if resource_type == "payments" and reference_doctype == "Payment Request":
		set_payment_request_status(event)


def set_mandate_status(event):
	mandates = []
	if isinstance(event["links"], (list,)):
		for link in event["links"]:
			mandates.append(link["mandate"])
	else:
		mandates.append(event["links"]["mandate"])

	if (
		event["action"] == "pending_customer_approval"
		or event["action"] == "pending_submission"
		or event["action"] == "submitted"
		or event["action"] == "active"
	):
		disabled = 0
	else:
		disabled = 1

	for mandate in mandates:
		frappe.db.set_value("GoCardless Mandate", mandate, "disabled", disabled)


def set_payment_request_status(event):
	event_action = event["action"]
	payment_request = event["resource_metadata"]["reference_document"]
	doc = frappe.get_doc("Payment Request", payment_request)
	if event_action == "confirmed" and doc.status != "Paid":
		doc.set_as_paid()
	if event_action == "cancelled" and doc.status != "Cancelled":
		doc.set_as_cancelled()
	if event_action == "failed" and doc.status != "Failed":
		doc.db_set("status", "Failed")
		try: # failed reason is a field in ERPNext version 16+, so it may not exist in the database
			doc.db_set("failed_reason", event["details"]["description"])
		except:
			pass


def authenticate_signature(r):
	"""Returns True if the received signature matches the generated signature"""
	received_signature = frappe.get_request_header("Webhook-Signature")

	if not received_signature:
		return False

	for key in get_webhook_keys():
		computed_signature = hmac.new(key.encode("utf-8"), r.get_data(), hashlib.sha256).hexdigest()
		if hmac.compare_digest(str(received_signature), computed_signature):
			return True

	return False


def get_webhook_keys():
	def _get_webhook_keys():
		webhook_keys = [
			d.webhooks_secret
			for d in frappe.get_all(
				"GoCardless Settings",
				fields=["webhooks_secret"],
			)
			if d.webhooks_secret
		]

		return webhook_keys

	return frappe.cache().get_value("gocardless_webhooks_secret", _get_webhook_keys)


def clear_cache():
	frappe.cache().delete_value("gocardless_webhooks_secret")
