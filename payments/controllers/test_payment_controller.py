import json
import unittest
from unittest.mock import patch

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
# Integration tests — require database, SDK-free demo gateway
# ---------------------------------------------------------------------------


class TestPaymentControllerLifecycle(IntegrationTestCase):
	"""Integration tests for the PaymentController lifecycle.

	Uses the SDK-free Payment Demo Settings gateway. Tests the orchestration
	layer: initiate -> proceed -> process_response.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		if not frappe.db.exists("Payment Demo Settings", "Payment Demo Settings"):
			demo = frappe.get_doc({"doctype": "Payment Demo Settings", "gateway_name": "Demo"})
			demo.flags.ignore_mandatory = True
			demo.insert(ignore_permissions=True)

		gateway_name = "Demo-Gateway"
		if not frappe.db.exists("Payment Gateway", gateway_name):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway",
					"gateway": gateway_name,
					"gateway_settings": "Payment Demo Settings",
					"gateway_controller": "Payment Demo Settings",
				}
			).insert(ignore_permissions=True)

		cls.gateway_name = gateway_name
		frappe.db.commit()

	# -- initiate --

	def test_initiate_creates_psl(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Created")
		stored = json.loads(psl.tx_data)
		self.assertEqual(stored["amount"], 25.00)

	def test_initiate_returns_controller_instance(self):
		tx_data = _make_tx_data()
		controller, _psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		self.assertIsInstance(controller, PaymentController)

	# -- proceed --

	def test_proceed_initiates_charge(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		proceeded = PaymentController.proceed(psl_name)
		self.assertEqual(proceeded.integration, "Payment Demo Settings")
		self.assertTrue(proceeded.payload["demo"])
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Initiated")
		self.assertEqual(psl.correlation_id, f"demo-{psl_name}")

	def test_proceed_is_idempotent(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		first = PaymentController.proceed(psl_name)
		second = PaymentController.proceed(psl_name)
		self.assertEqual(first.payload, second.payload)

	# -- process_response: success --

	def test_process_response_success(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		result = PaymentController.process_response(psl_name, response)
		self.assertEqual(result.indicator_color, "green")
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Paid")

	# -- process_response: declined --

	def test_process_response_declined(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(
			hash=None,
			message=None,
			payload={"status": "failed", "decline_reason": "Demo declined"},
		)
		result = PaymentController.process_response(psl_name, response)
		self.assertEqual(result.indicator_color, "red")
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Declined")

	# -- process_response: ref doc hook error --

	def test_process_response_ref_doc_hook_error(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		def exploding_hook(*args, **kwargs):
			raise ValueError("hook exploded")

		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		ref_doc_class = frappe.get_doc("User", "Administrator").__class__
		with patch.object(ref_doc_class, "on_payment_charge_processed", create=True, new=exploding_hook):
			PaymentController.process_response(psl_name, response)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Error - RefDoc")

	# -- pre_data_capture_hook --

	def test_pre_data_capture_hook_stores_state(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		data = PaymentController.pre_data_capture_hook(psl_name)
		self.assertIsInstance(data, dict)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Data Capture")
