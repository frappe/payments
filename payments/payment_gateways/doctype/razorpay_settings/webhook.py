# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

import frappe
from frappe import _
from frappe.utils import call_hook_method

SUPPORTED_WEBHOOK_EVENTS = {"refund.processed", "refund.failed"}


def process_webhook(raw_body: bytes, signature: str, event_id: str = "") -> str | None:
	"""Verify and log a webhook, returning the Integration Request name.

	Returns None for unhandled events. Razorpay resends an event until it gets a
	2xx, so a delivery it has already logged reuses that row rather than piling
	up duplicates. Subscribers must still make their own writes idempotent.
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

	logged = event_id and frappe.db.exists(
		"Integration Request", {"request_id": event_id, "request_description": "Refund Notification"}
	)
	if logged:
		return logged

	log = frappe.get_doc(
		{
			"doctype": "Integration Request",
			"integration_request_service": "Razorpay",
			"request_description": "Refund Notification",
			"request_id": event_id,
			"data": body,
			"is_remote_request": 1,
			"status": "Queued",
		}
	).insert(ignore_permissions=True)

	return log.name


# Razorpay is not a logged-in user, so this cannot be anything but a guest
# endpoint. It authenticates on the HMAC signature before it writes anything.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep
def razorpay_webhook():
	"""Answer 200 to anything Razorpay should not resend.

	A rejected or malformed request goes to the Error Log, because retrying it
	changes nothing and Razorpay disables an endpoint that keeps failing. A queue
	that is down is the opposite case: the error is left to reach Razorpay so the
	delivery comes back, and the event id keeps the retry from logging twice.
	"""
	event_id = frappe.get_request_header("X-Razorpay-Event-Id", "")

	try:
		name = process_webhook(
			frappe.request.data,
			frappe.get_request_header("X-Razorpay-Signature", ""),
			event_id,
		)
	except Exception as exception:
		frappe.db.rollback()
		frappe.log_error(
			f"Razorpay webhook rejected: {exception}",
			f"Razorpay event id: {event_id or 'unknown'}\n\n{frappe.get_traceback(with_context=True)}",
		)
		return

	if not name:
		return

	# The worker reads this row by name, so it has to be there before the job is.
	frappe.db.commit()  # nosemgrep: frappe-semgrep-rules.rules.frappe-manual-commit
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
