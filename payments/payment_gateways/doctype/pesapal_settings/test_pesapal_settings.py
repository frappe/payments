# Copyright (c) 2024, Frappe Technologies and contributors
# For license information, please see license.txt

import unittest
import frappe
from frappe.test_runner import make_test_records

from payments.payment_gateways.doctype.pesapal_settings.pesapal_settings import PesapalSettings


class TestPesapalSettings(unittest.TestCase):
	def setUp(self):
		# Create test Pesapal settings
		if not frappe.db.exists("Pesapal Settings", "Pesapal Settings"):
			pesapal_settings = frappe.get_doc({
				"doctype": "Pesapal Settings",
				"consumer_key": "test_consumer_key",
				"consumer_secret": "test_consumer_secret",
				"is_sandbox": 1,
				"ipn_notification_type": "POST"
			})
			pesapal_settings.flags.ignore_mandatory = True
			pesapal_settings.insert()

	def test_validate_transaction_currency(self):
		pesapal_settings = frappe.get_doc("Pesapal Settings")
		
		# Test supported currency
		try:
			pesapal_settings.validate_transaction_currency("KES")
		except Exception:
			self.fail("validate_transaction_currency raised an exception for supported currency")
		
		# Test unsupported currency
		with self.assertRaises(frappe.ValidationError):
			pesapal_settings.validate_transaction_currency("INR")

	def test_get_base_url(self):
		pesapal_settings = frappe.get_doc("Pesapal Settings")
		
		# Test sandbox URL
		pesapal_settings.is_sandbox = 1
		self.assertEqual(
			pesapal_settings.get_base_url(),
			"https://cybqa.pesapal.com/pesapalv3/api"
		)
		
		# Test production URL
		pesapal_settings.is_sandbox = 0
		self.assertEqual(
			pesapal_settings.get_base_url(),
			"https://pay.pesapal.com/v3/api"
		)

	def test_prepare_order_data(self):
		pesapal_settings = frappe.get_doc("Pesapal Settings")
		pesapal_settings.ipn_id = "test-ipn-id"
		
		test_data = {
			"order_id": "TEST001",
			"currency": "KES",
			"amount": 1000.0,
			"description": "Test payment",
			"payer_email": "test@example.com",
			"payer_name": "John Doe",
			"reference_doctype": "Sales Invoice",
			"reference_docname": "SI001"
		}
		
		order_data = pesapal_settings.prepare_order_data(test_data)
		
		self.assertEqual(order_data["id"], "TEST001")
		self.assertEqual(order_data["currency"], "KES")
		self.assertEqual(order_data["amount"], 1000.0)
		self.assertEqual(order_data["description"], "Test payment")
		self.assertEqual(order_data["notification_id"], "test-ipn-id")
		self.assertEqual(order_data["billing_address"]["email_address"], "test@example.com")
		self.assertEqual(order_data["billing_address"]["first_name"], "John")
		self.assertEqual(order_data["billing_address"]["last_name"], "Doe")

	def test_get_payment_url(self):
		pesapal_settings = frappe.get_doc("Pesapal Settings")

		payment_details = {
			"amount": 1000,
			"title": "Test Payment",
			"description": "Test payment description",
			"reference_doctype": "Sales Invoice",
			"reference_docname": "SI001",
			"payer_email": "test@example.com",
			"payer_name": "John Doe",
			"order_id": "TEST001",
			"currency": "KES",
			"payment_gateway": "Pesapal",
		}

		# This should create an integration request and return a URL
		url = pesapal_settings.get_payment_url(**payment_details)
		self.assertTrue(url.startswith("/"))
		self.assertIn("pesapal_checkout", url)
		self.assertIn("token=", url)

	def test_ipn_validation(self):
		from payments.payment_gateways.doctype.pesapal_settings.pesapal_utils import validate_ipn_request

		# Valid IPN data
		valid_ipn = {
			"OrderTrackingId": "test-tracking-id",
			"OrderMerchantReference": "TEST001",
			"OrderNotificationType": "IPNCHANGE"
		}
		self.assertTrue(validate_ipn_request(valid_ipn))

		# Invalid IPN data - missing required field
		invalid_ipn = {
			"OrderTrackingId": "test-tracking-id",
			"OrderNotificationType": "IPNCHANGE"
		}
		self.assertFalse(validate_ipn_request(invalid_ipn))

		# Invalid notification type
		invalid_type_ipn = {
			"OrderTrackingId": "test-tracking-id",
			"OrderMerchantReference": "TEST001",
			"OrderNotificationType": "INVALID"
		}
		self.assertFalse(validate_ipn_request(invalid_type_ipn))

	def test_payment_status_mapping(self):
		from payments.payment_gateways.doctype.pesapal_settings.pesapal_utils import get_payment_status_mapping

		mapping = get_payment_status_mapping()

		self.assertEqual(mapping["COMPLETED"], "Completed")
		self.assertEqual(mapping["FAILED"], "Failed")
		self.assertEqual(mapping["REVERSED"], "Cancelled")
		self.assertEqual(mapping["PENDING"], "Queued")
		self.assertEqual(mapping["INVALID"], "Failed")

	def tearDown(self):
		# Clean up test data
		if frappe.db.exists("Pesapal Settings", "Pesapal Settings"):
			frappe.delete_doc("Pesapal Settings", "Pesapal Settings", force=True)

		# Clean up any test integration requests
		test_requests = frappe.get_all(
			"Integration Request",
			filters={"integration_request_service": ["in", ["Pesapal", "Pesapal IPN"]]},
			pluck="name"
		)
		for request_name in test_requests:
			frappe.delete_doc("Integration Request", request_name, force=True)
