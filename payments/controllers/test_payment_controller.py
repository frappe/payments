import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.controllers import PaymentController
from payments.exceptions import PaymentControllerProcessingError, RefDocHookProcessingError
from payments.payments.doctype.payment_session_log.payment_session_log import PaymentSessionLog
from payments.types import GatewayProcessingResponse, TxData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tx_data(**overrides):
	"""Create a valid TxData for testing."""
	defaults = dict(
		amount=25.00,
		currency="USD",
		reference_doctype="User",
		reference_docname="Administrator",
		payer_contact={"email_id": "test@example.com"},
		payer_address={},
		loyalty_points=None,
		discount_amount=None,
	)
	defaults.update(overrides)
	return TxData(**defaults)


# ---------------------------------------------------------------------------
# Unit tests — no database required
# ---------------------------------------------------------------------------


class TestExceptionContracts(unittest.TestCase):
	"""Verify exception classes store all attributes correctly.

	Regression: RefDocHookProcessingError was called with 1 arg but
	constructor expects 2 — causing TypeError in error handling path.
	"""

	def test_ref_doc_hook_error_stores_both_attributes(self):
		err = RefDocHookProcessingError("hook failed", "charge")
		self.assertEqual(err.message, "hook failed")
		self.assertEqual(err.psltype, "charge")

	def test_ref_doc_hook_error_requires_two_args(self):
		with self.assertRaises(TypeError):
			RefDocHookProcessingError("only one arg")

	def test_processing_error_stores_both_attributes(self):
		err = PaymentControllerProcessingError("processing failed", "mandate")
		self.assertEqual(err.message, "processing failed")
		self.assertEqual(err.psltype, "mandate")

	def test_processing_error_requires_two_args(self):
		with self.assertRaises(TypeError):
			PaymentControllerProcessingError("only one arg")


class TestPSLContracts(unittest.TestCase):
	"""Verify PSL methods required by the controller exist.

	Regression: update_gateway_specific_state was called by
	pre_data_capture_hook but never defined on PaymentSessionLog.
	"""

	def test_update_gateway_specific_state_is_callable(self):
		self.assertTrue(hasattr(PaymentSessionLog, "update_gateway_specific_state"))
		self.assertTrue(callable(PaymentSessionLog.update_gateway_specific_state))


# ---------------------------------------------------------------------------
# Integration tests — require database + mocked Stripe SDK
# ---------------------------------------------------------------------------


STRIPE_MOCK_PATH = "payments.payment_gateways.doctype.stripe_settings.stripe_settings.stripe"


class TestPaymentControllerLifecycle(IntegrationTestCase):
	"""Integration tests for the PaymentController lifecycle.

	Uses StripeSettings as the concrete gateway with mocked Stripe SDK.
	Tests the orchestration layer: initiate -> proceed -> process_response.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# Create a Stripe Settings for testing (skip API validation)
		if not frappe.db.exists("Stripe Settings", "_Test Lifecycle"):
			settings = frappe.get_doc(
				{
					"doctype": "Stripe Settings",
					"gateway_name": "_Test Lifecycle",
					"publishable_key": "pk_test_lifecycle",
					"secret_key": "sk_test_lifecycle",
				}
			)
			settings.flags.ignore_mandatory = True
			settings.insert(ignore_permissions=True)

		# Ensure Payment Gateway record exists
		gateway_name = "Stripe-_Test Lifecycle"
		if not frappe.db.exists("Payment Gateway", gateway_name):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway",
					"gateway": gateway_name,
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "_Test Lifecycle",
				}
			).insert(ignore_permissions=True)

		cls.gateway_name = gateway_name
		frappe.db.commit()

	def _mock_intent(self, id="pi_test_123", status="succeeded", **kwargs):
		"""Create a mock Stripe PaymentIntent."""
		intent = MagicMock()
		intent.id = id
		intent.client_secret = f"{id}_secret_xyz"
		intent.status = status
		for k, v in kwargs.items():
			setattr(intent, k, v)
		return intent

	# -- initiate --

	@patch(STRIPE_MOCK_PATH)
	def test_initiate_creates_psl(self, mock_stripe):
		"""initiate() should create a PSL with status Created."""
		tx_data = _make_tx_data()

		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		self.assertIsNotNone(psl_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Created")
		# Verify tx_data is stored
		stored = json.loads(psl.tx_data)
		self.assertEqual(stored["amount"], 25.00)
		self.assertEqual(stored["currency"], "USD")

	@patch(STRIPE_MOCK_PATH)
	def test_initiate_returns_controller_instance(self, mock_stripe):
		"""initiate() should return a PaymentController subclass instance."""
		tx_data = _make_tx_data()

		controller, _psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		self.assertIsInstance(controller, PaymentController)

	# -- proceed --

	@patch(STRIPE_MOCK_PATH)
	def test_proceed_initiates_charge(self, mock_stripe):
		"""proceed() should call _initiate_charge and set PSL to Initiated."""
		mock_stripe.PaymentIntent.create.return_value = self._mock_intent()

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		proceeded = PaymentController.proceed(psl_name)

		self.assertIsNotNone(proceeded)
		self.assertEqual(proceeded.integration, "Stripe Settings")
		self.assertIn("client_secret", proceeded.payload)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Initiated")
		self.assertEqual(psl.correlation_id, "pi_test_123")

	@patch(STRIPE_MOCK_PATH)
	def test_proceed_is_idempotent(self, mock_stripe):
		"""proceed() called twice should return cached payload, not re-initiate."""
		mock_stripe.PaymentIntent.create.return_value = self._mock_intent()

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		first = PaymentController.proceed(psl_name)
		second = PaymentController.proceed(psl_name)

		# Stripe should only be called once
		mock_stripe.PaymentIntent.create.assert_called_once()
		self.assertEqual(first.payload, second.payload)

	# -- process_response: success --

	@patch(STRIPE_MOCK_PATH)
	def test_process_response_success(self, mock_stripe):
		"""process_response() with succeeded status should set PSL to Paid."""
		mock_stripe.PaymentIntent.create.return_value = self._mock_intent()
		mock_stripe.PaymentIntent.retrieve.return_value = self._mock_intent(status="succeeded")

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		response = GatewayProcessingResponse(
			hash=None,
			message=None,
			payload={"id": "pi_test_123", "status": "succeeded"},
		)
		result = PaymentController.process_response(psl_name, response)

		self.assertIsNotNone(result)
		self.assertEqual(result.indicator_color, "green")

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Paid")

	# -- process_response: declined --

	@patch(STRIPE_MOCK_PATH)
	def test_process_response_declined(self, mock_stripe):
		"""process_response() with declined status should set PSL to Declined."""
		mock_stripe.PaymentIntent.create.return_value = self._mock_intent()
		mock_stripe.PaymentIntent.retrieve.return_value = self._mock_intent(
			status="requires_payment_method"
		)

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		response = GatewayProcessingResponse(
			hash=None,
			message=None,
			payload={
				"id": "pi_test_123",
				"status": "requires_payment_method",
				"last_payment_error": {"message": "Card declined"},
			},
		)
		result = PaymentController.process_response(psl_name, response)

		self.assertIsNotNone(result)
		self.assertEqual(result.indicator_color, "red")

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Declined")

	# -- process_response: ref doc hook error (regression for bug 1) --

	@patch(STRIPE_MOCK_PATH)
	def test_process_response_ref_doc_hook_error(self, mock_stripe):
		"""RefDoc hook error should produce Error-RefDoc status, not crash.

		Regression: RefDocHookProcessingError was called with wrong arg
		count, causing TypeError instead of proper error handling.
		"""
		mock_stripe.PaymentIntent.create.return_value = self._mock_intent()
		mock_stripe.PaymentIntent.retrieve.return_value = self._mock_intent(status="succeeded")

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		# Patch the ref doc class to have a hook that raises
		def exploding_hook(*args, **kwargs):
			raise ValueError("hook exploded")

		response = GatewayProcessingResponse(
			hash=None,
			message=None,
			payload={"id": "pi_test_123", "status": "succeeded"},
		)

		ref_doc_class = frappe.get_doc("User", "Administrator").__class__
		with patch.object(
			ref_doc_class,
			"on_payment_charge_processed",
			create=True,
			new=exploding_hook,
		):
			PaymentController.process_response(psl_name, response)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Error - RefDoc")

	# -- pre_data_capture_hook (regression for bug 3) --

	@patch(STRIPE_MOCK_PATH)
	def test_pre_data_capture_hook_stores_state(self, mock_stripe):
		"""pre_data_capture_hook should call update_gateway_specific_state.

		Regression: update_gateway_specific_state did not exist on PSL.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		# pre_data_capture_hook calls _pre_data_capture_hook (returns {})
		# then calls psl.update_gateway_specific_state(data, "Data Capture")
		data = PaymentController.pre_data_capture_hook(psl_name)

		self.assertIsInstance(data, dict)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Data Capture")
