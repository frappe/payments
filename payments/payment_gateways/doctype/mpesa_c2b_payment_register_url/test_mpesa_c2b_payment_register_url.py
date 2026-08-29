# Copyright (c) 2024, Frappe Technologies and Contributors
# See license.txt

import frappe
import unittest
from unittest.mock import patch, Mock
from frappe.utils import get_request_site_address
from payments.payment_gateways.api.m_pesa_api import get_token
from payments.payment_gateways.doctype.mpesa_settings.test_mpesa_settings import TestMpesaSettings
from payments.payment_gateways.doctype.mpesa_c2b_payment_register_url.mpesa_c2b_payment_register_url import MpesaC2BPaymentRegisterURL


def create_mpesa_setting_doc():
	if not frappe.db.exists("Mpesa Settings", "Test Mpesa Settings"):
		mpesa_settings1 = frappe.new_doc("Mpesa Settings")
		mpesa_settings1.payment_gateway_name = "Test Mpesa Settings"
		mpesa_settings1.mpesa_environment = "sandbox"
		mpesa_settings1.consumer_key = "xMPJE16CDdAfBmOWvbqRsqlioAcQT77sWw2JD9OcceHp8fHv"
		mpesa_settings1.consumer_secret = "NDXh2tdne9bMrnOEZXd8gQZiHPMWSpfWc2YXBLGQxiz66OGbcn5S79DKakgt3LQN"
		mpesa_settings1.shortcode = "123456"
		mpesa_settings1.business_shortcode = "123456"
		mpesa_settings1.online_passkey = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"
		mpesa_settings1.till_number = "123456"
		mpesa_settings1.initiator_name = "test_initiator_name"
		mpesa_settings1.transaction_limit = 1000
		mpesa_settings1.security_credential = "test_security_credential"
		mpesa_settings1.sandbox=1
		mpesa_settings1.save()
		
def create_mpesa_c2b_payment_register_url_doc():
	if not frappe.db.exists("Mpesa C2B Payment Register URL", "Test Mpesa C2B Payment Register URL"):
		mpesa_c2b_payment_register_url = frappe.new_doc("Mpesa C2B Payment Register URL")
		mpesa_c2b_payment_register_url.business_shortcode = "123456"
		mpesa_c2b_payment_register_url.mpesa_settings = "Test Mpesa Settings"
		mpesa_c2b_payment_register_url.register_status = "Success"
		mpesa_c2b_payment_register_url.till_number = "123456"
		mpesa_c2b_payment_register_url.mode_of_payment = "Cash"
		mpesa_c2b_payment_register_url.company = "Pharma Express du 30 juin"
		mpesa_c2b_payment_register_url.save()
	
class TestMpesaC2BPaymentRegisterURL(TestMpesaSettings):
	def setUp(self):
		create_mpesa_setting_doc()
		create_mpesa_c2b_payment_register_url_doc()
  
	def tearDown(self):
		# Delete the Mpesa Settings document
		mpesa_settings_doc = frappe.get_doc("Mpesa C2B Payment Register URL", "Test Mpesa Settings")
		if mpesa_settings_doc:
			frappe.delete_doc("Mpesa C2B Payment Register URL", "Test Mpesa Settings")

		

	@patch('requests.post')
	def test_validate_success(self, mock_post):
		mock_post.return_value = Mock(status_code=200)
		mock_post.return_value.json.return_value = {
			"ResponseDescription": "Success"
		}
		mpesa=frappe.get_doc("Mpesa C2B Payment Register URL","Test Mpesa Settings")
		mpesa.validate()

		self.assertEqual(mpesa.register_status, "Success")

	@patch('requests.post')
	def test_validate_failure(self, mock_post):
		mock_post.return_value = Mock(status_code=200)
		mock_post.return_value.json.return_value = {
			"ResponseDescription": "Failure"
		}
		mpesa=frappe.get_doc("Mpesa C2B Payment Register URL","Test Mpesa Settings")
		mpesa.validate()

		self.assertEqual(mpesa.register_status, "Failed")

	@patch('requests.post')
	def test_validate_http_error(self, mock_post):
		mock_post.side_effect = Exception("HTTP Error")

		mpesa=frappe.get_doc("Mpesa C2B Payment Register URL","Test Mpesa Settings")
		mpesa.validate()
  
		self.assertEqual(mpesa.register_status, "Failed")
		

	@patch('requests.post')
	def test_validate_connection_error(self, mock_post):
		mock_post.side_effect = ConnectionError("Connection Error")

		mpesa=frappe.get_doc("Mpesa C2B Payment Register URL","Test Mpesa Settings")
		mpesa.validate()
  
		self.assertEqual(mpesa.register_status, "Failed")

	

