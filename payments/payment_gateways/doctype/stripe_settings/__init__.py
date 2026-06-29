# Copyright (c) Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE
#
# Stripe webhook endpoint; configure Stripe to POST here and set the signing secret.

import frappe

WEBHOOK_SECRET_CACHE_KEY = "stripe_webhook_secrets"


@frappe.whitelist(allow_guest=True)
def webhooks():
	r = frappe.request
	if not r:
		return

	payload = r.get_data()
	sig_header = frappe.get_request_header("Stripe-Signature")

	event, settings = construct_event(payload, sig_header)
	if event is None:
		# Bad/absent signature — tell Stripe to stop, do not process.
		frappe.local.response["http_status_code"] = 400
		return {"status": "invalid signature"}

	return handle_event(event, settings)


def construct_event(payload, sig_header):
	"""Verify the signature against every configured Stripe account's secret.

	Returns (event, settings_doc) on success, (None, None) on failure.
	"""
	import stripe

	if not sig_header:
		return None, None

	for settings_name, secret in get_webhook_secrets():
		try:
			event = stripe.Webhook.construct_event(payload, sig_header, secret)
			return event, frappe.get_doc("Stripe Settings", settings_name)
		except stripe.error.SignatureVerificationError:
			continue
		except frappe.DoesNotExistError:
			# Settings deleted since the cache was populated; refresh and skip it.
			clear_cache()
			continue
		except ValueError:
			# Malformed payload — no point trying other secrets.
			return None, None

	return None, None


def handle_event(event, settings):
	"""Dedupe on the Stripe event id, then route to the reconciler.

	A prior *Failed* attempt is allowed to reprocess (its log row is reused); any
	other prior status is a genuine duplicate and is skipped.
	"""
	event_id = event["id"]
	prior = frappe.db.get_value(
		"Stripe Webhook Log", {"stripe_event_id": event_id}, ["name", "status"], as_dict=True
	)
	if prior and prior.status != "Failed":
		return {"status": "duplicate"}

	if prior:
		log = frappe.get_doc("Stripe Webhook Log", prior.name)
	else:
		obj = event["data"]["object"]
		log = frappe.get_doc(
			{
				"doctype": "Stripe Webhook Log",
				"stripe_event_id": event_id,
				"event_type": event["type"],
				"stripe_object_id": obj.get("id"),
				"status": "Received",
				"stripe_settings": settings.name if settings else None,
				"payload": frappe.as_json(event),
			}
		)
		try:
			log.insert(ignore_permissions=True)
			frappe.db.commit()  # persist the dedupe row before doing any work
		except frappe.exceptions.DuplicateEntryError:
			# Concurrent delivery already inserted it.
			return {"status": "duplicate"}

	try:
		from payments.payment_gateways import stripe_reconcile

		result = stripe_reconcile.route_event(event, settings) or {}
		log.db_set("status", result.get("status_label", "Processed"), update_modified=False)
		if result.get("reference_doctype"):
			log.db_set("reference_doctype", result.get("reference_doctype"), update_modified=False)
			log.db_set("reference_name", result.get("reference_name"), update_modified=False)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		log.db_set("status", "Failed", update_modified=False)
		log.db_set("error", frappe.get_traceback(), update_modified=False)
		frappe.db.commit()
		frappe.log_error(frappe.get_traceback(), "Stripe webhook processing failed")
		# Transient failure — ask Stripe to retry; the dedupe above reprocesses the Failed row.
		frappe.local.response["http_status_code"] = 500
		return {"status": "error"}

	return {"status": "ok"}


def get_webhook_secrets():
	def _load():
		secrets = []
		for name in frappe.get_all("Stripe Settings", pluck="name"):
			doc = frappe.get_doc("Stripe Settings", name)
			secret = doc.get_password("webhook_secret", raise_exception=False)
			if secret:
				secrets.append((name, secret))
		return secrets

	return frappe.cache().get_value(WEBHOOK_SECRET_CACHE_KEY, _load) or []


def clear_cache():
	frappe.cache().delete_value(WEBHOOK_SECRET_CACHE_KEY)
