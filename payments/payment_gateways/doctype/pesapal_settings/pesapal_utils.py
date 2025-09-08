# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

"""
Utility functions for Pesapal payment gateway integration
"""

import json
import frappe
from frappe import _
from frappe.utils import now, add_to_date, get_datetime


def validate_ipn_request(ipn_data):
	"""Validate IPN request data"""
	required_fields = ["OrderTrackingId", "OrderMerchantReference", "OrderNotificationType"]
	
	for field in required_fields:
		if not ipn_data.get(field):
			frappe.log_error(f"Missing required field in IPN: {field}", "Pesapal IPN Validation Error")
			return False
	
	# Validate notification type
	if ipn_data.get("OrderNotificationType") not in ["IPNCHANGE", "CALLBACKURL"]:
		frappe.log_error(f"Invalid notification type: {ipn_data.get('OrderNotificationType')}", "Pesapal IPN Validation Error")
		return False
	
	return True


def log_ipn_request(ipn_data, status="Received"):
	"""Log IPN request for debugging and audit purposes"""
	try:
		log_doc = frappe.get_doc({
			"doctype": "Integration Request",
			"integration_type": "Remote",
			"integration_request_service": "Pesapal IPN",
			"status": status,
			"data": json.dumps(ipn_data, indent=2),
			"output": "",
			"error": "",
			"is_remote_request": 1
		})
		log_doc.insert(ignore_permissions=True)
		return log_doc.name
	except Exception as e:
		frappe.log_error(f"Failed to log IPN request: {str(e)}", "Pesapal IPN Logging Error")
		return None


def get_payment_status_mapping():
	"""Get mapping of Pesapal payment statuses to ERPNext statuses"""
	return {
		"COMPLETED": "Completed",
		"FAILED": "Failed", 
		"REVERSED": "Cancelled",
		"PENDING": "Queued",
		"INVALID": "Failed"
	}


def update_payment_request_status(integration_request, pesapal_response):
	"""Update payment request status based on Pesapal response"""
	try:
		payment_status = pesapal_response.get("payment_status_description", "").upper()
		status_mapping = get_payment_status_mapping()
		
		new_status = status_mapping.get(payment_status, "Queued")
		
		# Update integration request
		integration_request.update_status(pesapal_response, new_status)
		
		# Log the status update
		frappe.logger().info(f"Updated payment request {integration_request.name} status to {new_status}")
		
		return new_status
		
	except Exception as e:
		frappe.log_error(f"Failed to update payment request status: {str(e)}", "Pesapal Status Update Error")
		raise


def handle_payment_completion(integration_request, pesapal_response, status):
	"""Handle payment completion logic"""
	try:
		data = json.loads(integration_request.data)
		reference_doctype = data.get("reference_doctype")
		reference_docname = data.get("reference_docname")
		
		if reference_doctype and reference_docname:
			# Get the reference document
			doc = frappe.get_doc(reference_doctype, reference_docname)
			
			# Set payment data in flags for the document to access
			frappe.flags.payment_data = pesapal_response
			frappe.flags.payment_status = status
			
			# Call the document's payment handler if it exists
			if hasattr(doc, "on_payment_authorized"):
				doc.run_method("on_payment_authorized", status)
			
			# Create payment entry if payment is completed
			if status == "Completed":
				create_payment_entry(doc, pesapal_response, data)
				
		return True
		
	except Exception as e:
		frappe.log_error(f"Failed to handle payment completion: {str(e)}", "Pesapal Payment Completion Error")
		return False


def create_payment_entry(reference_doc, pesapal_response, payment_data):
	"""Create payment entry for completed payments"""
	try:
		# This is a simplified payment entry creation
		# In a real implementation, you would need to handle different document types
		# and create appropriate payment entries based on the reference document
		
		if payment_data.get("reference_doctype") == "Sales Invoice":
			# Create payment entry for sales invoice
			payment_entry = frappe.get_doc({
				"doctype": "Payment Entry",
				"payment_type": "Receive",
				"party_type": "Customer",
				"party": reference_doc.customer,
				"paid_amount": float(pesapal_response.get("amount", 0)),
				"received_amount": float(pesapal_response.get("amount", 0)),
				"reference_no": pesapal_response.get("confirmation_code"),
				"reference_date": get_datetime(pesapal_response.get("created_date")),
				"mode_of_payment": "Pesapal",
				"remarks": f"Payment via Pesapal - {pesapal_response.get('payment_method', 'Online')}"
			})
			
			# Add reference to the sales invoice
			payment_entry.append("references", {
				"reference_doctype": "Sales Invoice",
				"reference_name": reference_doc.name,
				"allocated_amount": float(pesapal_response.get("amount", 0))
			})
			
			payment_entry.insert(ignore_permissions=True)
			payment_entry.submit()
			
			frappe.logger().info(f"Created payment entry {payment_entry.name} for Pesapal payment")
			
	except Exception as e:
		frappe.log_error(f"Failed to create payment entry: {str(e)}", "Pesapal Payment Entry Error")


def validate_webhook_signature(request_data, signature):
	"""Validate webhook signature (if Pesapal provides signature validation)"""
	# Pesapal v3 doesn't seem to provide signature validation in their documentation
	# This is a placeholder for future implementation if they add it
	return True


def get_pesapal_settings():
	"""Get Pesapal settings document"""
	try:
		return frappe.get_doc("Pesapal Settings")
	except frappe.DoesNotExistError:
		frappe.throw(_("Pesapal Settings not found. Please configure Pesapal settings first."))


def is_duplicate_ipn(order_tracking_id, merchant_reference):
	"""Check if this IPN has already been processed"""
	try:
		# Check if we have already processed this IPN
		existing_logs = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": "Pesapal IPN",
				"data": ["like", f"%{order_tracking_id}%"],
				"status": "Completed"
			},
			limit=1
		)
		
		return len(existing_logs) > 0
		
	except Exception as e:
		frappe.log_error(f"Error checking duplicate IPN: {str(e)}", "Pesapal Duplicate Check Error")
		return False


def cleanup_old_integration_requests():
	"""Cleanup old integration requests (can be called via scheduled job)"""
	try:
		# Delete integration requests older than 30 days
		cutoff_date = add_to_date(now(), days=-30)
		
		old_requests = frappe.get_all(
			"Integration Request",
			filters={
				"integration_request_service": ["in", ["Pesapal", "Pesapal IPN"]],
				"creation": ["<", cutoff_date]
			},
			pluck="name"
		)
		
		for request_name in old_requests:
			frappe.delete_doc("Integration Request", request_name, ignore_permissions=True)
		
		frappe.logger().info(f"Cleaned up {len(old_requests)} old Pesapal integration requests")
		
	except Exception as e:
		frappe.log_error(f"Error cleaning up integration requests: {str(e)}", "Pesapal Cleanup Error")
