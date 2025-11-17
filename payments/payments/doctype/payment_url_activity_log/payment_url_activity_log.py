# Copyright (c) 2025, Frappe Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe import get_request_header
from frappe.utils import now_datetime, time_diff_in_seconds





class PaymentURLActivityLog(Document):
	pass


def create_payment_url_activity_log(
	order_id, payment_request, integration_request=None, access_type="Clicked", resource_payload=None
):
	try:
		request = frappe.local.request

		ip_address = request.remote_addr if request else None
		
		user_agent = get_request_header("User-Agent")
		
		referrer_url = get_request_header("Referer")
	
		session_id = frappe.session.sid if hasattr(frappe.session, "sid") else None

		status_before = frappe.db.get_value("Payment Request", payment_request, "status")

		existing_log_name = frappe.db.get_value(
			"Payment URL Activity Log",
			{
				"order_id": order_id,
				"payment_request": payment_request,
				"access_type": access_type,
			},
		)
		
		if existing_log_name:
			log = frappe.get_doc("Payment URL Activity Log", existing_log_name)
			
			last_time = log.access_timestamp
			if isinstance(last_time, str):
				from datetime import datetime
				last_time = datetime.fromisoformat(last_time)

			current_time = now_datetime()
			difference = current_time - last_time
			minutes_passed = difference.total_seconds() / 60

			if minutes_passed < 45:
					message = (
						f"As per Bank Muscat regulations, this payment link can only be used after the mandatory waiting period. "
						f"Please try again after {45 - int(minutes_passed)} minutes."
					)
					return None, message

			log.access_count = (log.access_count or 0) + 1
			log.access_timestamp = now_datetime()
			log.ip_address = ip_address
			log.user_agent = user_agent
			log.referrer_url = referrer_url
			log.session_id = session_id
			log.status_before = status_before

			log.save(ignore_permissions=True)
			
			return log, None
		
		log = frappe.new_doc("Payment URL Activity Log")
		log.order_id = order_id
		log.access_timestamp = now_datetime()
		log.payment_request = payment_request
		log.integration_request = integration_request
		log.request_url = referrer_url
		log.payment_url = ""
		log.resource_payload = resource_payload
		log.payload_before_encryption = ""
		log.access_count = 1
		log.access_type = access_type
		log.status_before = status_before
		log.ip_address = ip_address
		log.session_id = session_id
		log.user_agent = user_agent
		log.insert(ignore_permissions=True)

		return log, None

	except Exception as e:
		frappe.log_error(e.message)
		return None
