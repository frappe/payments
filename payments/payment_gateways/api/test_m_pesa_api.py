import unittest
from unittest.mock import patch, Mock
import json
from frappe.tests.utils import FrappeTestCase

from payments.payment_gateways.api.m_pesa_api import (
    get_token,
    confirmation,
    validation,
    get_mpesa_mode_of_payment,
    get_mpesa_draft_payments,
    submit_mpesa_payment,
)


class TestMPesaAPI(FrappeTestCase):
    
    @patch("requests.get")
    def test_get_token(self, mock_get):
        mock_response = Mock()
        mock_response.json.return_value = {"access_token": "dummy_token"}
        mock_get.return_value = mock_response

        app_key = "dummy_key"
        app_secret = "dummy_secret"
        base_url = "https://example.com"

        token = get_token(app_key, app_secret, base_url)

        self.assertEqual(token, "dummy_token")

    def test_confirmation(self):
        # Test accepted case
        args = {
            "TransactionType": "Payment",
            "TransID": "123456",
            "TransTime": "2024-05-01T12:00:00",
            "TransAmount": 100.0,
            "BusinessShortCode": "123456",
            "BillRefNumber": "BILL001",
            "InvoiceNumber": "INV001",
            "OrgAccountBalance": 500.0,
            "ThirdPartyTransID": "789012",
            "MSISDN": "1234567890",
            "FirstName": "John",
            "MiddleName": "Doe",
            "LastName": "Smith",
        }
        result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

        # Test rejected case
        args["TransAmount"] = "invalid_amount"
        result = confirmation(**args)
        self.assertEqual(result["ResultCode"], 1)
        self.assertEqual(result["ResultDesc"], "Rejected")

    def test_validation(self):
        # Test validation always returns accepted
        result = validation()
        self.assertEqual(result["ResultCode"], 0)
        self.assertEqual(result["ResultDesc"], "Accepted")

    @patch("frappe.get_all")
    def test_get_mpesa_mode_of_payment(self, mock_get_all):
        mock_get_all.return_value = [{"mode_of_payment": "Cash"}]

        company = "Test Company"

        modes_of_payment = get_mpesa_mode_of_payment(company)

        self.assertEqual(modes_of_payment, ["Cash"])

    @patch("frappe.get_all")
    def test_get_mpesa_draft_payments(self, mock_get_all):
        mock_get_all.return_value = [{"name": "MP001", "amount": 100.0}]

        company = "Test Company"
        mode_of_payment = "Cash"

        payments = get_mpesa_draft_payments(company, mode_of_payment)

        self.assertEqual(len(payments), 1)
        self.assertEqual(payments[0]["name"], "MP001")
        self.assertEqual(payments[0]["amount"], 100.0)

    @patch("frappe.get_doc")
    @patch("frappe.get_all")
    def test_submit_mpesa_payment(self, mock_get_all, mock_get_doc):
        mock_get_all.return_value = [{"name": "MP001"}]
        mock_get_doc.return_value = Mock(payment_entry="PE001")

        mpesa_payment = "MP001"
        customer = "Test Customer"

        payment_entry = submit_mpesa_payment(mpesa_payment, customer)

        self.assertEqual(payment_entry, "PE001")

