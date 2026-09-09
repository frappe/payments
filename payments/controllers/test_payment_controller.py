import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils.file_lock import LockTimeoutError

from payments.controllers import PaymentController
from payments.controllers.payment_controller import frontend_defaults
from payments.exceptions import (
	FailedToInitiateFlowError,
	PaymentControllerProcessingError,
	RefDocHookProcessingError,
)
from payments.payment_gateways.doctype.payment_demo_settings.payment_demo_settings import (
	PaymentDemoSettings,
)
from payments.payments.doctype.payment_session_log.payment_session_log import PaymentSessionLog
from payments.types import GatewayProcessingResponse, Initiated, TxData

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


class _ChargeCounter:
	def __init__(self):
		self.count = 0


@contextmanager
def _count_charges():
	"""Count real gateway initiations, so an idempotency assertion cannot pass
	merely because two returned payloads happen to be equal."""
	counter = _ChargeCounter()
	real = PaymentDemoSettings._initiate_charge

	def counting(self):
		counter.count += 1
		return real(self)

	with patch.object(PaymentDemoSettings, "_initiate_charge", counting):
		yield counter


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


class TestSubclassInvariants(unittest.TestCase):
	"""Verify PaymentController enforces its flowstates/frontend_defaults
	invariants at class-DEFINITION time via __init_subclass__ (not per
	instantiation)."""

	def test_missing_invariants_raise_at_class_definition(self):
		with self.assertRaises(TypeError):

			class _BadController(PaymentController):
				pass  # declares neither flowstates nor frontend_defaults

	def test_well_formed_subclass_defines_cleanly(self):
		from payments.types import FrontendDefaults, SessionStates

		# Should NOT raise at definition time.
		class _GoodController(PaymentController):
			flowstates = SessionStates(success=[], pre_authorized=[], processing=[], declined=[])
			frontend_defaults = FrontendDefaults(gateway_css="", gateway_js="", gateway_wrapper="")


class TestGatewayRef(IntegrationTestCase):
	def test_roundtrip_through_the_path_production_uses(self):
		"""create_log writes GatewayRef.to_json(); get_controller reads it back
		through PaymentSessionLog.parse_gateway_ref. Round-trip that pair, not a
		to_json/from_json symmetry no production code exercises."""
		from payments.payments.doctype.payment_session_log.payment_session_log import create_log
		from payments.types import GatewayRef

		ref = GatewayRef(gateway_settings="Stripe Settings", gateway_controller="acme")
		psl = create_log(tx_data=_make_tx_data())
		psl.db_set("gateway", ref.to_json(), commit=True)
		psl.reload()

		restored = GatewayRef(**psl.parse_gateway_ref(psl.gateway))
		self.assertEqual(restored.gateway_settings, "Stripe Settings")
		self.assertEqual(restored.gateway_controller, "acme")


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
		# No Payment Demo Settings fixture is needed: it is a Single, so it always
		# resolves through frappe.get_doc, and nothing here reads its fields.

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
		"""A second proceed() must not initiate a second charge at the gateway.

		Comparing the two returned payloads does NOT show that: the demo gateway
		derives its payload from the PSL name, so they are equal whether or not a
		second charge happened. Count the gateway calls instead.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		with _count_charges() as charges:
			first = PaymentController.proceed(psl_name)
			second = PaymentController.proceed(psl_name)

		self.assertEqual(charges.count, 1, "proceed() initiated a second charge at the gateway")
		self.assertEqual(first.payload, second.payload)

	def test_proceed_after_data_capture_does_not_recharge(self):
		"""pre_data_capture_hook moves the session to "Data Capture"; /pay then
		re-renders and calls proceed(). An idempotency guard keyed on
		status == "Initiated" stops matching there, and the payer is charged twice
		by simply reloading the page.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		PaymentController.pre_data_capture_hook(psl_name)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Data Capture")

		with _count_charges() as charges:
			PaymentController.proceed(psl_name)

		self.assertEqual(charges.count, 0, "a second charge was initiated after data capture")

	def test_data_capture_state_does_not_clobber_the_initiation_payload(self):
		"""The gateway's initiation response is what proceed() uses as its
		idempotency token, so data-capture state must be stored separately."""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		proceeded = PaymentController.proceed(psl_name)

		with patch.object(PaymentDemoSettings, "_pre_data_capture_hook", return_value={"capture": "probe"}):
			PaymentController.pre_data_capture_hook(psl_name)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(
			json.loads(psl.initiation_response_payload),
			proceeded.payload,
			"data-capture state overwrote the gateway initiation payload",
		)
		self.assertEqual(json.loads(psl.data_capture_payload), {"capture": "probe"})

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

	def test_processing_session_still_accepts_the_final_callback(self):
		"""A gateway that answers "pending" owes a second, final callback.

		If Processing is treated as final, process_response's post-lock guard
		short-circuits that callback and the payment is stuck in Processing for
		good — money taken, never reconciled.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		pending = GatewayProcessingResponse(hash=None, message=None, payload={"status": "pending"})
		PaymentController.process_response(psl_name, pending)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Processing")

		settled = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		result = PaymentController.process_response(psl_name, settled)

		self.assertEqual(
			frappe.get_doc("Payment Session Log", psl_name).status,
			"Paid",
			"the settling callback was dropped; the payment is stuck in Processing",
		)
		self.assertEqual(result.indicator_color, "green")

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

		# This assertion used to read status == "Error - RefDoc". That pinned the
		# defect: the gateway had captured the money, and overwriting Paid with a
		# reconciliation failure destroyed the only record of it, marked the
		# session settled so it could not be re-driven, and made it purgeable.
		# The gateway outcome and our bookkeeping are now separate fields.
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Paid")
		self.assertEqual(psl.reconciliation, "Failed")
		self.assertTrue(psl.reconciliation_error)
		self.assertFalse(psl.is_disposable())

	# -- process_response: error shape --

	def test_process_response_processing_error_shape(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		# Force a processing error by making the charge processor raise
		with patch.object(
			PaymentDemoSettings, "_process_response_for_charge", side_effect=ValueError("boom")
		):
			result = PaymentController.process_response(psl_name, response)
		self.assertEqual(result.indicator_color, "red")
		self.assertEqual(result.status_changed_to, frappe._("Server Error"))
		self.assertEqual(result.payload, {})
		self.assertIsInstance(result.action, dict)

	def test_process_response_unmapped_status_is_handled(self):
		"""A status that is in no flowstate category (success/pre_authorized/
		processing/declined) must surface as a clean error Processed, not an
		uncaught ValueError traceback that returns nothing to the frontend."""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		# "unknown" is not declared in PaymentDemoSettings.flowstates
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "unknown"})
		result = PaymentController.process_response(psl_name, response)
		self.assertEqual(result.indicator_color, "red")
		self.assertEqual(result.status_changed_to, frappe._("Server Error"))
		self.assertEqual(result.payload, {})
		self.assertIsInstance(result.action, dict)

	def test_a_processing_failure_does_not_make_the_session_rechargeable(self):
		"""The gateway has ALREADY answered by the time this handler runs — either
		its response could not be processed, or it reported a status we do not map
		(which may well be a success we failed to recognise). Money may be held.

		Writing "Error" there put the session into RETRYABLE_STATES while leaving
		initiation_response_payload intact, so the idempotency guard in
		_run_initiation stopped applying and the next proceed() initiated a SECOND
		real charge for the same session. "Error" was answering two questions:
		"initiation failed, nothing was sent, retry freely" and "the gateway
		answered and we could not understand it".
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		# A status in no flowstate category: the gateway spoke, we cannot map it.
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "unknown"})
		PaymentController.process_response(psl_name, response)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertTrue(psl.initiation_response_payload, "precondition: the charge was initiated")
		self.assertFalse(
			psl.may_retry_charge(),
			f"status {psl.status!r} lets a session the gateway already answered be re-charged",
		)

		calls = []
		original = PaymentDemoSettings._initiate_charge

		def _counting(inner_self):
			calls.append(1)
			return original(inner_self)

		with patch.object(PaymentDemoSettings, "_initiate_charge", _counting):
			PaymentController.proceed(psl_name)
		self.assertEqual(calls, [], "a second real gateway charge was initiated")

	def test_a_late_callback_can_still_resolve_a_processing_failure(self):
		"""The other half: not re-chargeable must not mean not recoverable. If the
		gateway resends something we CAN map, that has to be processed — so the
		state must stay out of SETTLED_STATES."""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		PaymentController.process_response(
			psl_name, GatewayProcessingResponse(hash=None, message=None, payload={"status": "unknown"})
		)

		PaymentController.process_response(
			psl_name, GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Paid")

	# -- process_response: lock contention --

	def test_process_response_lock_contention_is_reported_and_reraised(self):
		"""This asserted that contention returns a state-report Processed. That is
		wrong on the path that matters: a webhook caller reads a non-error return as
		accepted, answers 200, and the gateway stops resending — so if the holder
		then fails, the event is lost. Contention now re-raises so the gateway
		retries, and records an Error Log so an operator can see it happened.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})

		# The lock must raise from __enter__, not from the call that builds it.
		# frappe's filelock is a @contextmanager generator: calling it cannot raise,
		# so `side_effect=LockTimeoutError` only exercised `lock = filelock(...)` —
		# a statement that never raises in production. Mutation proved it: moving
		# lock.__enter__() outside the guarded try left this test green.
		@contextmanager
		def _contended(*args, **kwargs):
			raise LockTimeoutError("busy")
			yield  # pragma: no cover - makes this a generator

		before = frappe.db.count("Error Log")
		with patch("payments.controllers.payment_controller.filelock", _contended):
			with self.assertRaises(LockTimeoutError):
				PaymentController.process_response(psl_name, response)
		self.assertGreater(frappe.db.count("Error Log"), before)

	def test_proceed_redirects_on_initiation_failure(self):
		from payments.exceptions import FailedToInitiateFlowError

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		with patch.object(
			PaymentDemoSettings,
			"_initiate_charge",
			side_effect=FailedToInitiateFlowError("nope", {"err": 1}),
		):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Error")

	def test_proceed_redirects_on_http_error(self):
		"""On an HTTPError during initiation, proceed must read the response body
		off the exception (v2 never sets frappe.flags.integration_request) and
		redirect — not raise AttributeError that masks the original error."""
		from requests.exceptions import HTTPError

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		class _FakeResponse:
			def json(self):
				return {"gateway_error": "boom"}

		http_error = HTTPError("502 Bad Gateway")
		http_error.response = _FakeResponse()

		with patch.object(PaymentDemoSettings, "_initiate_charge", side_effect=http_error):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Error")

	def test_an_unexpected_initiation_failure_leaves_a_retryable_session(self):
		"""The arm that catches everything else wrote no status at all, so the
		session stayed at "Started" — which is not may_retry_charge() — while the
		PREVIOUS attempt's initiation_response_payload survived. Every later
		proceed() then returned that dead payload and made zero gateway calls: the
		payer could never pay and nothing surfaced it.

		Reachable by an ordinary network blip: ConnectionError and Timeout are not
		HTTPError subclasses, so they land here and not in the arm above.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		# A first attempt that really initiated, so there is a payload to go stale.
		PaymentController.proceed(psl_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertTrue(psl.initiation_response_payload)
		# "Error" is the retryable state: initiation failed before the gateway was
		# reached, so re-using this session is safe.
		psl.db_set("status", "Error", commit=True)

		with patch.object(
			PaymentDemoSettings, "_initiate_charge", side_effect=ConnectionError("connection reset")
		):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)

		psl.reload()
		self.assertEqual(psl.status, "Error", "an unexpected initiation failure recorded no outcome")
		self.assertTrue(psl.may_retry_charge(), "the session was left unable to retry")

		# The consequence, asserted rather than inferred: a retry reaches the gateway.
		calls = []
		original = PaymentDemoSettings._initiate_charge

		def _counting(inner_self):
			calls.append(1)
			return original(inner_self)

		with patch.object(PaymentDemoSettings, "_initiate_charge", _counting):
			PaymentController.proceed(psl_name)
		self.assertEqual(len(calls), 1, "the retry returned a stale payload instead of charging")

	def test_a_charge_that_could_not_be_recorded_is_never_recharged(self):
		"""The gateway call succeeded and persisting its result failed.

		The initiation flow wrote the gateway metadata (correlation_id, flow_type)
		and the payload+status in TWO separate commits, so a failure between them
		left a session that had been charged with no idempotency token — and the
		catch-all then marked it "Error", which is RETRYABLE_STATES, so the next
		proceed() charged the payer a second time. A failure AFTER the gateway has
		been reached is not the same thing as one before it.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		with patch.object(PaymentSessionLog, "record_initiation", side_effect=RuntimeError("db went away")):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Unresolved", "a charged session was left in a retryable state")
		self.assertFalse(psl.may_retry_charge())

		# The charge really happened, so its details must be recoverable by hand.
		latest = frappe.get_all(
			"Error Log",
			filters={"reference_name": psl_name},
			fields=["error"],
			order_by="creation desc",
			limit=1,
		)[0]
		self.assertIn("demo", latest.error, "the unrecorded gateway payload was not preserved")

		# And the consequence that matters.
		calls = []
		original = PaymentDemoSettings._initiate_charge

		def _counting(inner_self):
			calls.append(1)
			return original(inner_self)

		with patch.object(PaymentDemoSettings, "_initiate_charge", _counting):
			with self.assertRaises(Exception):
				PaymentController.proceed(psl_name)
		self.assertEqual(calls, [], "the payer was charged a second time")

	def test_a_failed_recording_leaves_no_half_written_session(self):
		"""Atomicity, asserted by a failure that lands mid-record.

		The payload is made unserialisable, so `frappe.as_json` raises while the
		write is being assembled. With one db_set that happens BEFORE anything is
		committed, so nothing is persisted. With the previous two-commit shape the
		gateway metadata had already landed, leaving a session carrying a
		correlation id for a charge with no recorded payload — the half-state
		another request could observe.
		"""

		class _Unserialisable:
			pass

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		before = frappe.get_doc("Payment Session Log", psl_name)
		self.assertIsNone(before.correlation_id, "precondition")

		bad = Initiated(correlation_id="demo-half-written", payload={"obj": _Unserialisable()})
		with patch.object(PaymentDemoSettings, "_initiate_charge", return_value=bad):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertIsNone(
			psl.correlation_id,
			"the gateway metadata was committed separately from the payload, so the "
			"session records a charge it has no payload for",
		)
		self.assertIsNone(psl.initiation_response_payload)

	def test_an_interrupted_attempt_is_never_charged_again(self):
		"""The residual of the two fixes above: not an exception, but the process
		simply stopping between the gateway accepting the charge and
		record_initiation committing it — a SIGKILL, a worker timeout, a container
		eviction. The session is left at "Started" with no payload, which is
		non-terminal and has no idempotency token, so both retry guards are
		bypassed and the next proceed() charges the payer again.

		The state below is set directly because that is exactly what a dead process
		leaves behind; there is no way to assert on the manner of death, only on
		the state it produces. Note which states can legitimately reach here:
		"Created" means never attempted, and every failure path now writes "Error"
		or "Unresolved" — so "Started" with no payload can only be an interrupted
		attempt.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		psl.db_set({"status": "Started", "initiation_response_payload": None}, commit=True)

		calls = []
		original = PaymentDemoSettings._initiate_charge

		def _counting(inner_self):
			calls.append(1)
			return original(inner_self)

		with patch.object(PaymentDemoSettings, "_initiate_charge", _counting):
			with self.assertRaises(Exception):
				PaymentController.proceed(psl_name)

		self.assertEqual(calls, [], "an interrupted attempt was charged a second time")
		psl.reload()
		self.assertEqual(psl.status, "Unresolved", "the interrupted attempt was left chargeable")
		self.assertFalse(psl.may_retry_charge())

	def test_a_failure_before_the_gateway_does_not_block_the_payment(self):
		"""The status "Started" was written before the pre-flight work (load_state,
		_patch_tx_data), so a failure there left the session at "Started" with no
		payload — indistinguishable from an interrupted charge, even though the
		gateway had demonstrably not been called. The interrupted-attempt guard then
		marked it "Unresolved" on the next attempt and blocked the payment for good.

		"Started" has to mean "we are about to call the gateway", or it cannot be
		used to answer "might we have called it?".
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		with patch.object(PaymentDemoSettings, "_patch_tx_data", side_effect=ValueError("schema drift")):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertFalse(
			psl.has_an_unrecorded_attempt(),
			f"a pre-gateway failure left the session at {psl.status!r}, which reads as a charge in flight",
		)

		# The payer must still be able to pay.
		proceeded = PaymentController.proceed(psl_name)
		self.assertTrue(proceeded.payload)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Initiated")

	def test_a_redirect_from_patch_tx_data_does_not_block_the_payment(self):
		"""The same hole reached through a DESIGNED path: `except frappe.Redirect:
		raise` exists so a controller's _patch_tx_data can send the payer somewhere
		first. The gateway is not called, and the payer is expected to come back —
		at which point the session must still be payable."""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		with patch.object(PaymentDemoSettings, "_patch_tx_data", side_effect=frappe.Redirect):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertFalse(psl.has_an_unrecorded_attempt())

		proceeded = PaymentController.proceed(psl_name)
		self.assertTrue(proceeded.payload)

	def test_the_replay_path_handles_a_state_failure_too(self):
		"""Rebuilding TxData can fail on schema drift, and both call sites need the
		same handling. Only the fresh path had it, so a payer returning to an
		already-initiated session got a bare exception out of proceed() and the
		operator got no diagnostic — and the replay path is the MORE likely one to
		hit drift, being the one that reads a session written by an older release.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertTrue(psl.initiation_response_payload, "precondition: the replay path is taken")

		before = frappe.db.count("Error Log")
		with patch.object(PaymentDemoSettings, "_patch_tx_data", side_effect=ValueError("schema drift")):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)
		self.assertGreater(frappe.db.count("Error Log"), before, "the replay path failed with no diagnostic")

	def test_started_never_carries_a_previous_attempts_payload(self):
		"""has_an_unrecorded_attempt() reads "Started with no payload" as a request
		that died mid-charge. That only works if `Started` is written with the
		payload cleared — and the two gateway-failure arms STORE one
		(`set_initiation_payload(err.data, "Error")`). A retry after such a failure
		therefore ran with a stale payload attached, so an interruption there was
		invisible to the fingerprint, and the idempotency guard then replayed the
		previous attempt's error blob as though it were an initiation response:
		the session sticks at `Started` for good and the payer can never pay.

		Asserted at the moment it matters — inside the gateway call — rather than
		by simulating a kill, because the invariant is about the state the call
		runs under.
		"""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		# Attempt 1 fails in a way that stores a payload and stays retryable.
		with patch.object(
			PaymentDemoSettings,
			"_initiate_charge",
			side_effect=FailedToInitiateFlowError("declined at init", {"gateway_error": "nope"}),
		):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Error")
		self.assertTrue(psl.initiation_response_payload, "precondition: a payload was stored")

		seen = {}
		original = PaymentDemoSettings._initiate_charge

		def _capture(inner_self):
			row = frappe.get_doc("Payment Session Log", psl_name)
			seen["status"] = row.status
			seen["payload"] = row.initiation_response_payload
			seen["unrecorded"] = row.has_an_unrecorded_attempt()
			return original(inner_self)

		with patch.object(PaymentDemoSettings, "_initiate_charge", _capture):
			PaymentController.proceed(psl_name)

		self.assertEqual(seen["status"], "Started")
		self.assertIsNone(
			seen["payload"], "the gateway was called with a superseded attempt's payload attached"
		)
		self.assertTrue(
			seen["unrecorded"],
			"an interruption during this call would not have been detectable as an unrecorded charge",
		)

	def test_a_fresh_session_is_not_mistaken_for_an_interrupted_one(self):
		"""The control that keeps the guard above from blocking every payment:
		"Created" is a session that has never been attempted, and it must charge
		normally."""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Created")

		proceeded = PaymentController.proceed(psl_name)
		self.assertTrue(proceeded.payload)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Initiated")

	def test_initiation_is_recorded_in_one_write(self):
		"""Control: the whole result of a successful initiation lands together, so
		no other request can observe half of it."""
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		PaymentController.proceed(psl_name)

		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Initiated")
		self.assertTrue(psl.initiation_response_payload)
		self.assertTrue(psl.correlation_id)
		self.assertEqual(psl.flow_type, "charge")
		self.assertIsNone(psl.processing_response_payload)

	def test_a_gateway_http_error_records_its_own_diagnostic(self):
		"""The HTTPError arm created no Error Log and handed error_ref the most
		recent row in the whole table, so the code the payer is told to quote named
		an unrelated failure — and frappe.get_last_doc raises DoesNotExistError on
		an empty table, from inside the except, so a fresh site 500'd instead of
		redirecting."""
		from requests.exceptions import HTTPError

		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)

		class _FakeResponse:
			def json(self):
				return {"gateway_error": "boom"}

		http_error = HTTPError("502 Bad Gateway")
		http_error.response = _FakeResponse()

		before = frappe.db.count("Error Log")
		with patch.object(PaymentDemoSettings, "_initiate_charge", side_effect=http_error):
			with self.assertRaises(frappe.Redirect):
				PaymentController.proceed(psl_name)

		self.assertEqual(
			frappe.db.count("Error Log") - before, 1, "the arm recorded no diagnostic of its own"
		)
		latest = frappe.get_all(
			"Error Log", fields=["method", "reference_name"], order_by="creation desc", limit=1
		)[0]
		self.assertIn("initiation", latest.method.lower())
		self.assertEqual(latest.reference_name, psl_name, "the log was not linked to this session")

	# -- pre_data_capture_hook --

	def test_pre_data_capture_hook_stores_state(self):
		tx_data = _make_tx_data()
		_controller, psl_name = PaymentController.initiate(tx_data, self.gateway_name)
		data = PaymentController.pre_data_capture_hook(psl_name)
		self.assertIsInstance(data, dict)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Data Capture")


class TestFrontendDefaultsEndpoint(IntegrationTestCase):
	"""frontend_defaults is a whitelisted endpoint reachable from the Payment
	Button form script, so its argument handling is externally driven.

	It carries a `doctype: str` annotation, and @frappe.whitelist() wraps an
	annotated function in validate_argument_types (frappe/__init__.py:463) under
	`_in_request_or_test()`. Whether the wrapper or the function's own isinstance
	check rejects a bad argument therefore depends on the calling context, so this
	accepts either rejection — the point being that a non-str is rejected and no
	gateway configuration is returned, not which guard did it.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		gateway_name = "Demo-Gateway-Defaults"
		if not frappe.db.exists("Payment Gateway", gateway_name):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway",
					"gateway": gateway_name,
					"gateway_settings": "Payment Demo Settings",
					"gateway_controller": "Payment Demo Settings",
				}
			).insert(ignore_permissions=True)

	def test_returns_declared_defaults_for_a_registered_gateway(self):
		defaults = frontend_defaults("Payment Demo Settings")
		self.assertEqual(defaults["gateway_wrapper"], "<div id='demo-gateway'></div>")
		self.assertEqual(defaults["gateway_css"], "")
		self.assertEqual(defaults["gateway_js"], "")

	def test_rejects_a_doctype_that_is_not_a_registered_gateway(self):
		with self.assertRaises(frappe.ValidationError):
			frontend_defaults("User")

	def test_rejects_a_non_string_argument(self):
		for bad in (123, ["Payment Demo Settings"], None):
			with self.subTest(bad=bad):
				with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
					frontend_defaults(bad)

	def test_rejects_bytes_rather_than_coercing_them(self):
		"""Guards against a silent widening: if argument coercion ever starts
		accepting bytes, a b"..." payload would reach get_controller as a str."""
		with self.assertRaises((frappe.ValidationError, frappe.exceptions.FrappeTypeError)):
			frontend_defaults(b"Payment Demo Settings")


class TestMutedServerToServerErrors(IntegrationTestCase):
	"""On the server-to-server (webhook) path, an error must still return a
	Processed.

	Returning None reads as success to most webhook handlers, which then answer
	HTTP 200 and the gateway stops retrying — for a payment the app has just
	recorded as Error. The muted flag should suppress user-facing prose, not the
	caller's ability to tell failure from success.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# No Payment Demo Settings fixture is needed: it is a Single, so it always
		# resolves through frappe.get_doc, and nothing here reads its fields.
		gateway_name = "Demo-Gateway-S2S"
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

	def _initiated_psl(self):
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		return psl_name

	def test_processing_error_returns_a_processed_when_muted(self):
		psl_name = self._initiated_psl()
		response = GatewayProcessingResponse(
			hash=None, message=None, payload={"status": "succeeded", "s2s": True}
		)
		with patch.object(
			PaymentDemoSettings, "_process_response_for_charge", side_effect=ValueError("boom")
		):
			result = PaymentController.process_response(psl_name, response)

		self.assertIsNotNone(
			result, "muted path returned None; a webhook caller cannot tell failure from success"
		)
		self.assertEqual(result.indicator_color, "red")
		# "Unresolved", not "Error". This test used to assert "Error", which put a
		# session into RETRYABLE_STATES while its initiation payload was still in
		# place — and this case is the sharpest illustration of why that is wrong:
		# the gateway payload literally says "succeeded" and only OUR processing of
		# it threw. Money moved. "Error" means initiation never reached the gateway.
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Unresolved")
		self.assertFalse(psl.may_retry_charge(), "a session the gateway settled was left re-chargeable")

	def test_ref_doc_hook_error_returns_a_processed_when_muted(self):
		psl_name = self._initiated_psl()
		response = GatewayProcessingResponse(
			hash=None, message=None, payload={"status": "succeeded", "s2s": True}
		)

		def exploding_hook(*args, **kwargs):
			raise ValueError("hook exploded")

		ref_doc_class = frappe.get_doc("User", "Administrator").__class__
		with patch.object(ref_doc_class, "on_payment_charge_processed", create=True, new=exploding_hook):
			result = PaymentController.process_response(psl_name, response)

		self.assertIsNotNone(result, "muted ref-doc-hook failure returned None")
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Paid", "the gateway's Paid outcome was overwritten")
		self.assertEqual(psl.reconciliation, "Failed")

	def test_unmuted_path_is_unchanged(self):
		"""Control: without the s2s flag the caller still gets the rich message."""
		psl_name = self._initiated_psl()
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		with patch.object(
			PaymentDemoSettings, "_process_response_for_charge", side_effect=ValueError("boom")
		):
			result = PaymentController.process_response(psl_name, response)
		self.assertIsNotNone(result)
		self.assertEqual(result.indicator_color, "red")


class TestUnexpectedGatewayHookFailures(IntegrationTestCase):
	"""A gateway's own hooks must not be able to escape process_response.

	process_response catches only PayloadIntegrityError,
	PaymentControllerProcessingError and RefDocHookProcessingError. Anything else
	from a gateway implementation — a signature library raising ValueError, a
	None dereference in a decline-message renderer — propagates out, leaving the
	session at "Initiated" with no Error Log. On the webhook path that is a 500,
	so the gateway retries forever into the same crash, and "Initiated" is not
	final so nothing surfaces or purges it: a reachable state with no exit.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# No Payment Demo Settings fixture is needed: it is a Single, so it always
		# resolves through frappe.get_doc, and nothing here reads its fields.
		gateway_name = "Demo-Gateway-Hooks"
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

	def _initiated_psl(self):
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		return psl_name

	def test_validate_response_raising_is_handled(self):
		psl_name = self._initiated_psl()
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		with patch.object(
			PaymentDemoSettings, "_validate_response", side_effect=RuntimeError("signature lib blew up")
		):
			result = PaymentController.process_response(psl_name, response)

		self.assertIsNotNone(result, "_validate_response's exception escaped process_response")
		self.assertEqual(result.indicator_color, "red")
		self.assertNotEqual(
			frappe.get_doc("Payment Session Log", psl_name).status,
			"Initiated",
			"session was left at Initiated with no way out",
		)

	def test_render_failure_message_raising_is_handled(self):
		"""On the declined path _render_failure_message is evaluated inside the
		db_set dict, outside the nested guard further down."""
		psl_name = self._initiated_psl()
		response = GatewayProcessingResponse(
			hash=None, message=None, payload={"status": "failed", "decline_reason": "nope"}
		)
		with patch.object(
			PaymentDemoSettings, "_render_failure_message", side_effect=RuntimeError("renderer blew up")
		):
			result = PaymentController.process_response(psl_name, response)

		self.assertIsNotNone(result, "_render_failure_message's exception escaped process_response")
		self.assertNotEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Initiated")

	def test_is_server_to_server_raising_is_handled(self):
		"""mute is computed in the outer try, which has only a finally."""
		psl_name = self._initiated_psl()
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		with patch.object(
			PaymentDemoSettings, "_is_server_to_server", side_effect=RuntimeError("no payload")
		):
			result = PaymentController.process_response(psl_name, response)

		self.assertIsNotNone(result, "_is_server_to_server's exception escaped process_response")
		# also pin the actual claim: processing continued on the interactive path
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Paid")
		self.assertEqual(result.indicator_color, "green")


class TestProceedSerialisesInitiation(IntegrationTestCase):
	"""Two concurrent proceed() calls on one session must not both charge.

	A DB row lock cannot protect this: update_tx_data commits before
	_initiate_charge, releasing the lock before the very call it needs to guard.
	So initiation runs under frappe.utils.synchronization.filelock — the
	primitive frappe designates for process synchronisation, and which is not
	transaction-scoped — and the idempotency guard is re-read INSIDE the lock, so
	whichever caller loses the race returns the winner's payload rather than
	initiating again.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		gateway_name = "Demo-Gateway-Lock"
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

	def test_initiation_runs_under_a_session_scoped_lock(self):
		"""The lock name must identify the session, or unrelated payments
		needlessly serialise against each other."""
		from frappe.utils.synchronization import filelock as real_filelock

		taken = []

		@contextmanager
		def spy(lock_name, **kwargs):
			taken.append(lock_name)
			with real_filelock(lock_name, **kwargs):
				yield

		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		with patch("payments.controllers.payment_controller.filelock", spy):
			PaymentController.proceed(psl_name)

		self.assertTrue(taken, "proceed() initiated without taking any lock")
		self.assertIn(psl_name, taken[0], f"lock {taken[0]!r} is not scoped to this session")

	def test_losing_the_race_returns_the_winners_payload_without_recharging(self):
		"""Simulates the winner completing while we waited on the lock: the guard
		must be re-read inside the lock, so the loser makes no gateway call."""
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		winner_payload = {"demo": True, "psl": psl_name, "winner": True}

		@contextmanager
		def lock_during_which_the_winner_commits(lock_name, **kwargs):
			frappe.get_doc("Payment Session Log", psl_name).set_initiation_payload(
				winner_payload, "Initiated"
			)
			yield

		with patch("payments.controllers.payment_controller.filelock", lock_during_which_the_winner_commits):
			with _count_charges() as charges:
				result = PaymentController.proceed(psl_name)

		self.assertEqual(charges.count, 0, "the loser of the race initiated a second charge")
		self.assertEqual(result.payload, winner_payload)


class TestReconciliationIsSeparateFromGatewayOutcome(IntegrationTestCase):
	"""status records what the GATEWAY did; reconciliation records what WE did.

	Overwriting a captured payment's "Paid" with "Error - RefDoc" when the ref
	doc hook fails destroys the only record that the money moved, and marks the
	session settled so it can neither be re-driven nor retained.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		gateway_name = "Demo-Gateway-Recon"
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

	def _paid_session(self, hook=None):
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		response = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		ref_doc_class = frappe.get_doc("User", "Administrator").__class__
		if hook is None:
			PaymentController.process_response(psl_name, response)
		else:
			with patch.object(ref_doc_class, "on_payment_charge_processed", create=True, new=hook):
				PaymentController.process_response(psl_name, response)
		return frappe.get_doc("Payment Session Log", psl_name)

	def test_hook_failure_keeps_the_paid_status(self):
		def exploding_hook(*args, **kwargs):
			raise ValueError("Payment Entry creation failed")

		psl = self._paid_session(hook=exploding_hook)
		self.assertEqual(psl.status, "Paid", "the gateway's Paid outcome was overwritten")
		self.assertEqual(psl.reconciliation, "Failed")
		self.assertTrue(psl.reconciliation_error, "no Error Log was linked for the operator")
		# The Link alone is not enough: frappe clears Error Log after 14 days while
		# this row is retained until reconciliation succeeds, so the diagnostic has
		# to live on the log itself.
		self.assertIn(
			"Payment Entry creation failed",
			psl.reconciliation_error_message or "",
			"the failure text was not kept on the log",
		)

	def test_hook_success_marks_reconciliation_done(self):
		def good_hook(*args, **kwargs):
			return None

		psl = self._paid_session(hook=good_hook)
		self.assertEqual(psl.status, "Paid")
		self.assertEqual(psl.reconciliation, "Done")

	def test_unreconciled_payment_is_never_purged(self):
		"""An unreconciled captured payment is exactly the audit trail we must
		keep, however old it is."""

		def exploding_hook(*args, **kwargs):
			raise ValueError("boom")

		psl = self._paid_session(hook=exploding_hook)
		frappe.db.set_value(
			"Payment Session Log", psl.name, "modified", "2000-01-01 00:00:00", update_modified=False
		)
		PaymentSessionLog.clear_old_logs(days=90)
		self.assertTrue(
			frappe.db.exists("Payment Session Log", psl.name),
			"a captured but unreconciled payment was deleted",
		)


class TestDeclinedIsNotSettled(IntegrationTestCase):
	"""Whether a PSP can settle a session it already declined is unknown, so the
	design must be safe either way: a late callback is accepted (no lost money)
	while a declined log is still purged by age (no unbounded growth).

	Those two only conflicted while retention and the processing guard keyed on
	the same set.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		gateway_name = "Demo-Gateway-Declined"
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

	def test_a_late_settlement_after_a_decline_is_accepted(self):
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		declined = GatewayProcessingResponse(
			hash=None, message=None, payload={"status": "failed", "decline_reason": "try again"}
		)
		PaymentController.process_response(psl_name, declined)
		self.assertEqual(frappe.get_doc("Payment Session Log", psl_name).status, "Declined")

		settled = GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		PaymentController.process_response(psl_name, settled)
		self.assertEqual(
			frappe.get_doc("Payment Session Log", psl_name).status,
			"Paid",
			"a settlement arriving after a decline was dropped",
		)

	def test_a_declined_log_is_still_purged_by_age(self):
		"""Control: accepting late callbacks must not make declined sessions
		immortal — that is what the retention sweep exists to prevent."""
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		declined = GatewayProcessingResponse(
			hash=None, message=None, payload={"status": "failed", "decline_reason": "nope"}
		)
		PaymentController.process_response(psl_name, declined)
		frappe.db.set_value(
			"Payment Session Log", psl_name, "modified", "2000-01-01 00:00:00", update_modified=False
		)
		PaymentSessionLog.clear_old_logs(days=90)
		self.assertFalse(frappe.db.exists("Payment Session Log", psl_name))


class TestTamperWhitelistIsObservable(IntegrationTestCase):
	"""A rejected tx_data update is an attempted tamper on a money path, so it has
	to leave a trace an operator can actually find.

	frappe.logger("payments").warning() does not: measured on test_site_2, that
	logger's effective level is ERROR, isEnabledFor(WARNING) is False, and it
	carries file-only handlers with propagate=False — so the warning was dropped
	entirely and nothing reached the Error Log or any job output.
	"""

	TITLE = "Rejected non-whitelisted tx_data update"

	def _rows(self):
		"""Count only our own rows. A global Error Log count is non-deterministic —
		anything else in the process writing one moves it — which made these two
		tests flaky in both directions."""
		return frappe.db.count("Error Log", {"method": self.TITLE})

	def test_rejected_keys_reach_the_error_log(self):
		before = self._rows()
		filtered = PaymentController._filter_tx_data_updates(
			{"payer_contact": {"full_name": "ok"}, "amount": 1, "currency": "XXX"}
		)
		self.assertEqual(filtered, {"payer_contact": {"full_name": "ok"}})
		self.assertGreater(self._rows(), before, "the rejected tamper attempt left no trace")

	def test_nothing_is_logged_when_every_key_is_allowed(self):
		"""Control: the normal path must not create Error Log noise."""
		before = self._rows()
		PaymentController._filter_tx_data_updates({"payer_contact": {"full_name": "ok"}})
		self.assertEqual(self._rows(), before)


class TestProceedNeverStartsASecondCharge(IntegrationTestCase):
	"""Once the gateway holds money or owes an answer, proceed() must return the
	initiation it already has rather than starting another charge.

	The guard must not key on is_terminal(): that is the DISPLAY question, and a
	Paid session is display-terminal, so keying on it fell straight through into
	a fresh initiation — one real gateway charge, Paid -> Initiated, and the
	processing payload nulled. Only a state that finished WITHOUT the gateway
	holding money (Declined, Error) may re-initiate.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		gateway_name = "Demo-Gateway-NoRecharge"
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

	def _session_in(self, status):
		"""An initiated session forced to `status`, so it holds a payload."""
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		frappe.db.set_value("Payment Session Log", psl_name, "status", status)
		return psl_name

	def test_states_where_the_gateway_holds_money_are_never_recharged(self):
		for status in ("Paid", "Authorized", "Processing", "Cancelled"):
			with self.subTest(status=status):
				psl_name = self._session_in(status)
				with _count_charges() as charges:
					PaymentController.proceed(psl_name)
				self.assertEqual(charges.count, 0, f"proceed() re-charged a {status} session")
				self.assertEqual(
					frappe.db.get_value("Payment Session Log", psl_name, "status"),
					status,
					f"proceed() overwrote the {status} status",
				)

	def test_a_paid_session_keeps_its_processing_payload(self):
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		PaymentController.process_response(
			psl_name, GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		)
		PaymentController.proceed(psl_name)
		psl = frappe.get_doc("Payment Session Log", psl_name)
		self.assertEqual(psl.status, "Paid")
		self.assertTrue(psl.processing_response_payload, "the record of the payment was destroyed")

	def test_only_a_session_that_never_reached_the_gateway_may_retry_in_place(self):
		"""`Error` means initiation failed before the gateway was reached, so no
		charge exists and re-using the session is safe.

		`Declined` is different and used to be treated the same. A decline is a
		gateway ANSWER, so a charge attempt exists — and this branch deliberately
		keeps `Declined` out of SETTLED_STATES, because a PSP that later settles a
		session it declined must not be ignored or money is lost. Both premises
		are defensible; together they mean an in-place retry can leave two live
		charges behind one session, with `record_initiation` overwriting the first
		one's correlation id and clearing its processing payload. A retry after a
		decline is therefore a NEW session, initiated by the reference document.
		"""
		errored = self._session_in("Error")  # outside the counter: setup charges too
		with _count_charges() as charges:
			PaymentController.proceed(errored)
		self.assertEqual(charges.count, 1, "an Error session could not retry")

		declined = self._session_in("Declined")
		with _count_charges() as charges:
			PaymentController.proceed(declined)
		self.assertEqual(
			charges.count, 0, "a declined session started a second charge the first could still settle"
		)


class TestBothEntryPointsShareOneLock(IntegrationTestCase):
	"""proceed() and process_response() contend for the same resource, so they
	must take the same lock. Two different lock names are never mutually
	exclusive, which is what let a webhook complete between /pay's terminal check
	and its call to proceed().
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		gateway_name = "Demo-Gateway-OneLock"
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

	def test_the_two_entry_points_take_the_same_lock_name(self):
		from frappe.utils.synchronization import filelock as real_filelock

		taken = []

		@contextmanager
		def spy(lock_name, **kwargs):
			taken.append(lock_name)
			with real_filelock(lock_name, **kwargs):
				yield

		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		with patch("payments.controllers.payment_controller.filelock", spy):
			PaymentController.proceed(psl_name)
			PaymentController.process_response(
				psl_name,
				GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"}),
			)

		self.assertEqual(len(taken), 2, f"expected both entry points to lock, got {taken}")
		self.assertEqual(taken[0], taken[1], f"different locks: {taken}")
		self.assertIn(psl_name, taken[0])


class TestProcessResponseFailuresBeforeTheInnerTry(IntegrationTestCase):
	"""The catch-all added for gateway hooks sits on the INNER try. Everything
	before it — reloading the PSL, rebuilding TxData, fetching the reference
	document — runs in the outer try, which has only a finally. A failure there
	escapes and strands the session at "Initiated" with no Error Log, which is
	exactly the state the catch-all exists to prevent.
	"""

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		gateway_name = "Demo-Gateway-Outer"
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

	def _initiated(self):
		_controller, psl_name = PaymentController.initiate(_make_tx_data(), self.gateway_name)
		PaymentController.proceed(psl_name)
		return psl_name

	def test_a_missing_reference_document_is_handled(self):
		"""Ordinary: the reference doc was deleted or renamed between initiate
		and the gateway's callback."""
		psl_name = self._initiated()
		psl = frappe.get_doc("Payment Session Log", psl_name)
		tx = json.loads(psl.tx_data)
		tx["reference_docname"] = "no-such-user@example.com"
		psl.db_set("tx_data", frappe.as_json(tx), commit=True)

		result = PaymentController.process_response(
			psl_name, GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		)
		self.assertIsNotNone(result, "a deleted reference document escaped process_response")
		self.assertNotEqual(
			frappe.db.get_value("Payment Session Log", psl_name, "status"),
			"Initiated",
			"the session was stranded at Initiated",
		)

	def test_an_unreconstructable_tx_data_is_handled(self):
		"""Schema drift: a stored tx_data key that TxData no longer accepts."""
		psl_name = self._initiated()
		psl = frappe.get_doc("Payment Session Log", psl_name)
		tx = json.loads(psl.tx_data)
		tx["unexpected_field"] = "from an older release"
		psl.db_set("tx_data", frappe.as_json(tx), commit=True)

		result = PaymentController.process_response(
			psl_name, GatewayProcessingResponse(hash=None, message=None, payload={"status": "succeeded"})
		)
		self.assertIsNotNone(result, "an unreconstructable tx_data escaped process_response")
		self.assertNotEqual(frappe.db.get_value("Payment Session Log", psl_name, "status"), "Initiated")
