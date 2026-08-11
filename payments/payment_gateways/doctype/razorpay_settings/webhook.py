# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.utils import call_hook_method

SUPPORTED_WEBHOOK_EVENTS = {"refund.processed", "refund.failed"}


def process_webhook(raw_body: bytes, signature: str) -> str | None:
	"""Verify and log a webhook, returning the Integration Request name.

	Returns None for unhandled events. The same event arrives more than once, so
	subscribers of `handle_refund_notification` must make their writes idempotent.
	"""
	controller = frappe.get_cached_doc("Razorpay Settings")
	secret = controller.get_password("webhook_secret", raise_exception=False)

	if not secret:
		# Verifying against an empty key would accept anything anyone signs.
		frappe.throw(_("Set a Webhook Secret in Razorpay Settings to accept webhooks"))

	body = raw_body.decode()
	controller.verify_signature(body, signature, secret)

	if frappe.parse_json(body).get("event") not in SUPPORTED_WEBHOOK_EVENTS:
		return None

	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": "Razorpay",
			"request_description": "Refund Notification",
			"data": body,
			"is_remote_request": 1,
			"status": "Queued",
		}
	).insert(ignore_permissions=True)

	return log.name


@frappe.whitelist(allow_guest=True, methods=["POST"])
def razorpay_webhook():
	"""Accept every webhook and answer 200.

	Razorpay disables an endpoint that keeps failing, so a rejected or malformed
	request goes to the Error Log rather than back to Razorpay as an error.
	"""
	try:
		name = process_webhook(
			frappe.request.data,
			frappe.get_request_header("X-Razorpay-Signature", ""),
		)
	except Exception:
		frappe.db.rollback()
		frappe.log_error("Razorpay webhook rejected")
		return

	if not name:
		return

	frappe.db.commit()
	frappe.enqueue(
		method="payments.payment_gateways.doctype.razorpay_settings.webhook.handle_refund_notification",
		queue="short",
		doctype="Integration Request",
		docname=name,
	)


def handle_refund_notification(doctype, docname):
	log = frappe.get_doc(doctype, docname)

	try:
		call_hook_method("handle_refund_notification", doctype=doctype, docname=docname)
	except Exception:
		frappe.db.rollback()
		frappe.log_error("Razorpay refund notification failed")
		log.handle_failure({"traceback": frappe.get_traceback()})
	else:
		log.db_set("status", "Completed")
