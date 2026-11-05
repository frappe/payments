# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import UnitTestCase

from payments.payment_gateways.doctype.paypal_settings.paypal_settings import (
	PayPalSettings,
	confirm_payment,
	create_recurring_profile,
	get_express_checkout_details,
	make_nvp_request,
	manage_recurring_payment_profile_status,
	parse_nvp_response,
	validate_ipn_request,
)


class TestPayPalNVPParser(UnitTestCase):
	def test_parse_nvp_response_from_str(self):
		nvp_str = (
			"TOKEN=EC%2d02517989P97306737&TIMESTAMP=2026%2d09%2d03T17%3a52%3a36Z"
			"&CORRELATIONID=0540cbc4072e1&ACK=Success&VERSION=98&BUILD=59149706"
		)
		parsed = parse_nvp_response(nvp_str)
		self.assertIsInstance(parsed, dict)
		self.assertEqual(parsed.get("ACK"), ["Success"])
		self.assertEqual(parsed.get("TOKEN"), ["EC-02517989P97306737"])
		self.assertEqual(parsed.get("CORRELATIONID"), ["0540cbc4072e1"])

	def test_parse_nvp_response_from_bytes(self):
		nvp_bytes = b"ACK=Success&TOKEN=EC%2dTEST1234"
		parsed = parse_nvp_response(nvp_bytes)
		self.assertIsInstance(parsed, dict)
		self.assertEqual(parsed.get("ACK"), ["Success"])
		self.assertEqual(parsed.get("TOKEN"), ["EC-TEST1234"])

	def test_parse_nvp_response_passthrough_dict(self):
		input_dict = {"ACK": ["Success"], "TOKEN": ["EC-EXISTING"]}
		parsed = parse_nvp_response(input_dict)
		self.assertEqual(parsed, input_dict)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	def test_make_nvp_request_parses_string_response(self, mock_post):
		mock_post.return_value = "ACK=Success&PAYMENTINFO_0_TRANSACTIONID=9B888542VH0894702&CORRELATIONID=123"
		result = make_nvp_request("https://api-3t.sandbox.paypal.com/nvp", data={"METHOD": "DoExpressCheckoutPayment"})

		mock_post.assert_called_once_with(
			url="https://api-3t.sandbox.paypal.com/nvp",
			data={"METHOD": "DoExpressCheckoutPayment"},
		)
		self.assertEqual(result.get("ACK"), ["Success"])
		self.assertEqual(result.get("PAYMENTINFO_0_TRANSACTIONID"), ["9B888542VH0894702"])
		self.assertEqual(result.get("CORRELATIONID"), ["123"])


class TestPayPalSettingsExecution(UnitTestCase):
	def setUp(self):
		self.paypal_settings = PayPalSettings.__new__(PayPalSettings)
		self.paypal_settings.paypal_sandbox = 1
		self.paypal_settings.use_sandbox = 1
		self.paypal_settings.api_username = "test_user"
		self.paypal_settings.api_password = "test_password"
		self.paypal_settings.signature = "test_sig"

	@patch.object(PayPalSettings, "get_paypal_params_and_url")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	def test_validate_paypal_credentials_success(self, mock_post, mock_params):
		mock_params.return_value = ({"USER": "test"}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_post.return_value = "ACK=Success&BUILD=12345"

		# Should execute without throwing an exception
		self.paypal_settings.validate_paypal_credentails()

	@patch.object(PayPalSettings, "get_paypal_params_and_url")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.throw")
	def test_validate_paypal_credentials_failure(self, mock_throw, mock_post, mock_params):
		mock_params.return_value = ({"USER": "test"}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_post.return_value = "ACK=Failure&L_ERRORCODE0=10002"

		self.paypal_settings.validate_paypal_credentails()
		mock_throw.assert_called_once()

	@patch.object(PayPalSettings, "get_paypal_params_and_url")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.throw")
	def test_validate_paypal_credentials_missing_ack(self, mock_throw, mock_post, mock_params):
		# Malformed response with no ACK key (e.g. ERROR=unavailable) must be rejected
		mock_params.return_value = ({"USER": "test"}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_post.return_value = "ERROR=unavailable"

		self.paypal_settings.validate_paypal_credentails()
		mock_throw.assert_called_once()

	@patch.object(PayPalSettings, "get_paypal_params_and_url")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.throw")
	def test_validate_paypal_credentials_unexpected_ack(self, mock_throw, mock_post, mock_params):
		# Unexpected/non-success ACK must be rejected
		mock_params.return_value = ({"USER": "test"}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_post.return_value = "ACK=UnexpectedStatus"

		self.paypal_settings.validate_paypal_credentails()
		mock_throw.assert_called_once()

	@patch.object(PayPalSettings, "get_paypal_params_and_url")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	def test_execute_set_express_checkout_success(self, mock_post, mock_params):
		mock_params.return_value = ({"USER": "test"}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_post.return_value = "ACK=Success&TOKEN=EC%2d02517989P97306737&CORRELATIONID=0540cbc4072e1"

		res = self.paypal_settings.execute_set_express_checkout(
			amount=50.0,
			currency="USD",
			description="Test Payment",
		)
		self.assertEqual(res.get("ACK"), ["Success"])
		self.assertEqual(res.get("TOKEN"), ["EC-02517989P97306737"])
		self.assertEqual(res.get("CORRELATIONID"), ["0540cbc4072e1"])

	@patch.object(PayPalSettings, "get_paypal_params_and_url")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.throw")
	def test_execute_set_express_checkout_missing_token(self, mock_throw, mock_post, mock_params):
		mock_params.return_value = ({"USER": "test"}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_post.return_value = "ACK=Success&CORRELATIONID=0540cbc4072e1"

		self.paypal_settings.execute_set_express_checkout(
			amount=50.0,
			currency="USD",
			description="Test Payment",
		)
		mock_throw.assert_called_once()

	@patch.object(PayPalSettings, "get_paypal_params_and_url")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.throw")
	def test_execute_set_express_checkout_empty_token(self, mock_throw, mock_post, mock_params):
		mock_params.return_value = ({"USER": "test"}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_post.return_value = "ACK=Success&TOKEN=&CORRELATIONID=0540cbc4072e1"

		self.paypal_settings.execute_set_express_checkout(
			amount=50.0,
			currency="USD",
			description="Test Payment",
		)
		mock_throw.assert_called_once()


class TestPayPalFlows(UnitTestCase):
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_redirect_uri", return_value="/test-redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.get_doc")
	def test_get_express_checkout_details_success(self, mock_get_doc, mock_post, mock_update, mock_get_redirect):
		mock_doc = MagicMock()
		mock_doc.get_paypal_params_and_url.return_value = ({}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_get_doc.return_value = mock_doc

		mock_post.return_value = "ACK=Success&PAYERID=PAYER123&EMAIL=buyer%40example.com&CORRELATIONID=CORR123"

		frappe.local.response = {}
		get_express_checkout_details("EC-TOKEN-123")

		self.assertEqual(frappe.local.response.get("type"), "redirect")
		self.assertEqual(frappe.local.response.get("location"), "/test-redirect")
		mock_update.assert_called_once_with(
			"EC-TOKEN-123",
			{"payerid": "PAYER123", "payer_email": "buyer@example.com"},
			"Authorized",
			doc=mock_doc,
		)

	@patch("frappe.respond_as_web_page")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.get_doc")
	def test_get_express_checkout_details_missing_payerid(self, mock_get_doc, mock_post, mock_update, mock_respond):
		mock_doc = MagicMock()
		mock_doc.get_paypal_params_and_url.return_value = ({}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_get_doc.return_value = mock_doc

		# ACK is Success but PAYERID is missing
		mock_post.return_value = "ACK=Success&EMAIL=buyer%40example.com&CORRELATIONID=CORR123"

		frappe.local.response = {}
		get_express_checkout_details("EC-TOKEN-123")

		# Must NOT authorize the request
		mock_update.assert_not_called()
		self.assertIsNone(frappe.local.response.get("location"))
		mock_respond.assert_called_once()

	@patch("frappe.respond_as_web_page")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("frappe.get_doc")
	def test_get_express_checkout_details_empty_payerid(self, mock_get_doc, mock_post, mock_update, mock_respond):
		mock_doc = MagicMock()
		mock_doc.get_paypal_params_and_url.return_value = ({}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_get_doc.return_value = mock_doc

		# ACK is Success but PAYERID is empty
		mock_post.return_value = "ACK=Success&PAYERID=&EMAIL=buyer%40example.com&CORRELATIONID=CORR123"

		frappe.local.response = {}
		get_express_checkout_details("EC-TOKEN-123")

		mock_update.assert_not_called()
		self.assertIsNone(frappe.local.response.get("location"))
		mock_respond.assert_called_once()

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.setup_redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_paypal_and_transaction_details")
	def test_confirm_payment_success(self, mock_details, mock_post, mock_update, mock_setup_redirect):
		mock_details.return_value = (
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			{},
			"https://api-3t.sandbox.paypal.com/nvp",
		)
		mock_post.return_value = "ACK=Success&PAYMENTINFO_0_TRANSACTIONID=TXN123&CORRELATIONID=CORR123"

		confirm_payment("EC-TOKEN-123")

		mock_update.assert_called_once_with(
			"EC-TOKEN-123",
			{"transaction_id": "TXN123", "correlation_id": "CORR123"},
			"Completed",
		)
		mock_setup_redirect.assert_called_once_with(
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			"payment-success?doctype=None&docname=None",
			None,
		)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.setup_redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_paypal_and_transaction_details")
	def test_confirm_payment_missing_transaction_id(self, mock_details, mock_post, mock_update, mock_setup_redirect):
		mock_details.return_value = (
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			{},
			"https://api-3t.sandbox.paypal.com/nvp",
		)
		# ACK is Success but PAYMENTINFO_0_TRANSACTIONID is missing
		mock_post.return_value = "ACK=Success&CORRELATIONID=CORR123"

		confirm_payment("EC-TOKEN-123")

		# Must NOT mark the payment Completed
		mock_update.assert_not_called()
		mock_setup_redirect.assert_called_once_with(
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			"payment-failed",
			None,
		)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.setup_redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_paypal_and_transaction_details")
	def test_confirm_payment_empty_transaction_id(self, mock_details, mock_post, mock_update, mock_setup_redirect):
		mock_details.return_value = (
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			{},
			"https://api-3t.sandbox.paypal.com/nvp",
		)
		# ACK is Success but PAYMENTINFO_0_TRANSACTIONID is empty
		mock_post.return_value = "ACK=Success&PAYMENTINFO_0_TRANSACTIONID=&CORRELATIONID=CORR123"

		confirm_payment("EC-TOKEN-123")

		mock_update.assert_not_called()
		mock_setup_redirect.assert_called_once_with(
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			"payment-failed",
			None,
		)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.setup_redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_paypal_and_transaction_details")
	def test_confirm_payment_failure_ack(self, mock_details, mock_post, mock_update, mock_setup_redirect):
		mock_details.return_value = (
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			{},
			"https://api-3t.sandbox.paypal.com/nvp",
		)
		mock_post.return_value = "ACK=Failure&PAYMENTINFO_0_TRANSACTIONID=TXN123"

		confirm_payment("EC-TOKEN-123")

		mock_update.assert_not_called()
		mock_setup_redirect.assert_called_once_with(
			{"payerid": "PAYER123", "amount": 100.0, "currency": "USD"},
			"payment-failed",
			None,
		)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.ZoneInfo")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_datetime")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.setup_redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_paypal_and_transaction_details")
	def test_create_recurring_profile_success(
		self, mock_details, mock_post, mock_update, mock_setup_redirect, mock_get_datetime, mock_zoneinfo
	):
		mock_dt = MagicMock()
		mock_dt.replace.return_value.astimezone.return_value.isoformat.return_value = "2026-01-01T00:00:00Z"
		mock_get_datetime.return_value = mock_dt

		mock_details.return_value = (
			{
				"description": "Subscription",
				"subscription_details": {
					"billing_period": "Month",
					"billing_frequency": 1,
					"subscription_amount": 10.0,
					"upfront_amount": 0.0,
					"start_date": "2026-01-01",
				},
				"currency": "USD",
			},
			{},
			"https://api-3t.sandbox.paypal.com/nvp",
		)
		mock_post.return_value = "ACK=Success&PROFILEID=PROFILE123"

		create_recurring_profile("EC-TOKEN-123", "PAYER123")

		mock_update.assert_called_once_with(
			"EC-TOKEN-123",
			{"profile_id": "PROFILE123"},
			"Completed",
		)
		mock_setup_redirect.assert_called_once_with(
			mock_details.return_value[0],
			"payment-success?doctype=None&docname=None",
			None,
		)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.ZoneInfo")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_datetime")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.setup_redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_paypal_and_transaction_details")
	def test_create_recurring_profile_missing_profile_id(
		self, mock_details, mock_post, mock_update, mock_setup_redirect, mock_get_datetime, mock_zoneinfo
	):
		mock_dt = MagicMock()
		mock_dt.replace.return_value.astimezone.return_value.isoformat.return_value = "2026-01-01T00:00:00Z"
		mock_get_datetime.return_value = mock_dt

		mock_details.return_value = (
			{
				"description": "Subscription",
				"subscription_details": {
					"billing_period": "Month",
					"billing_frequency": 1,
					"subscription_amount": 10.0,
					"upfront_amount": 0.0,
					"start_date": "2026-01-01",
				},
				"currency": "USD",
			},
			{},
			"https://api-3t.sandbox.paypal.com/nvp",
		)
		# ACK is Success but PROFILEID is missing
		mock_post.return_value = "ACK=Success"

		create_recurring_profile("EC-TOKEN-123", "PAYER123")

		# Must NOT mark Completed
		mock_update.assert_not_called()
		mock_setup_redirect.assert_called_once_with(
			mock_details.return_value[0],
			"payment-failed",
			None,
		)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.ZoneInfo")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_datetime")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.setup_redirect")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.update_integration_request_status")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.get_paypal_and_transaction_details")
	def test_create_recurring_profile_empty_profile_id(
		self, mock_details, mock_post, mock_update, mock_setup_redirect, mock_get_datetime, mock_zoneinfo
	):
		mock_dt = MagicMock()
		mock_dt.replace.return_value.astimezone.return_value.isoformat.return_value = "2026-01-01T00:00:00Z"
		mock_get_datetime.return_value = mock_dt

		mock_details.return_value = (
			{
				"description": "Subscription",
				"subscription_details": {
					"billing_period": "Month",
					"billing_frequency": 1,
					"subscription_amount": 10.0,
					"upfront_amount": 0.0,
					"start_date": "2026-01-01",
				},
				"currency": "USD",
			},
			{},
			"https://api-3t.sandbox.paypal.com/nvp",
		)
		# ACK is Success but PROFILEID is empty
		mock_post.return_value = "ACK=Success&PROFILEID="

		create_recurring_profile("EC-TOKEN-123", "PAYER123")

		mock_update.assert_not_called()
		mock_setup_redirect.assert_called_once_with(
			mock_details.return_value[0],
			"payment-failed",
			None,
		)

	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	def test_manage_recurring_payment_profile_status_ignore_11556(self, mock_post):
		mock_post.return_value = "ACK=Failure&L_ERRORCODE0=11556"

		# Should not throw when error code is 11556 (already cancelled / inactive)
		manage_recurring_payment_profile_status(
			"PROFILE123", "Cancel", {}, "https://api-3t.sandbox.paypal.com/nvp"
		)

	@patch("frappe.get_doc")
	@patch("payments.payment_gateways.doctype.paypal_settings.paypal_settings.make_post_request")
	def test_validate_ipn_request_success(self, mock_post, mock_get_doc):
		mock_doc = MagicMock()
		mock_doc.get_paypal_params_and_url.return_value = ({}, "https://api-3t.sandbox.paypal.com/nvp")
		mock_get_doc.return_value = mock_doc
		mock_post.return_value = "ACK=Success"

		# Should not throw
		validate_ipn_request({"recurring_payment_id": "PROFILE123"})
