# Copyright (c) 2021, Frappe and Contributors
# See LICENSE

import json
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.controllers import PaymentController
from payments.payments.doctype.payment_session_log.payment_session_log import (
	PaymentSessionLog,
	create_log,
	select_button,
)
from payments.types import TxData
from payments.utils import error_ref


def _make_tx_data() -> TxData:
	return TxData(
		amount=25.00,
		currency="EUR",
		reference_doctype="User",
		reference_docname="Administrator",
		payer_contact={},
		payer_address={},
		loyalty_points=None,
		discount_amount=None,
	)


class TestPaymentSessionLogTerminalStates(unittest.TestCase):
	"""Unit tests for terminal state methods."""

	def test_is_terminal_returns_true_for_paid(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Paid"
		self.assertTrue(psl.is_terminal())

	def test_is_terminal_returns_true_for_declined(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Declined"
		self.assertTrue(psl.is_terminal())

	def test_is_terminal_returns_false_for_created(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Created"
		self.assertFalse(psl.is_terminal())

	def test_is_terminal_returns_false_for_initiated(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Initiated"
		self.assertFalse(psl.is_terminal())

	def test_get_indicator_color_returns_green_for_paid(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Paid"
		self.assertEqual(psl.get_indicator_color(), "green")

	def test_get_indicator_color_returns_red_for_error(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Error"
		self.assertEqual(psl.get_indicator_color(), "red")

	def test_get_indicator_color_returns_gray_for_unknown(self):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "SomeUnknownStatus"
		self.assertEqual(psl.get_indicator_color(), "gray")


class TestSelectButtonCSRFHardening(unittest.TestCase):
	"""M3: the guest select_button endpoint must be POST-only. Frappe skips
	CSRF for guest sessions, so restricting to POST blocks the trivial
	cross-origin GET/link/<img> vectors."""

	def test_select_button_is_post_only(self):
		methods = frappe.allowed_http_methods_for_whitelisted_func.get(select_button)
		self.assertIsNotNone(methods, "select_button is not registered as whitelisted")
		self.assertEqual(methods, ["POST"])
		self.assertNotIn("GET", methods)


class TestErrorRef(IntegrationTestCase):
	"""The guest-facing correlation code must be findable in the Error Log.

	This class previously fed a plain `str` — an input production never supplies
	— and asserted `ref == value[-8:]`, restating the implementation. That made
	it blind in both directions: production emitted `str(error_log)[-8:]`, and
	Document.__str__ is "Error Log (name)", so the payer was quoting 7 characters
	of the name plus a stray ")" — a code present in no Error Log, which broke
	the support path this was supposed to provide. Pass a real document and
	assert the property.
	"""

	def test_the_code_is_a_suffix_of_the_error_log_name(self):
		error_log = frappe.log_error(title="probe for error ref", message="x")
		ref = error_ref(error_log)
		self.assertEqual(len(ref), 8)
		self.assertTrue(
			error_log.name.endswith(ref),
			f"{ref!r} is not findable in Error Log {error_log.name!r}",
		)

	def test_the_full_name_is_not_disclosed(self):
		"""M4: frappe's internal naming must not reach the guest."""
		error_log = frappe.log_error(title="probe for error ref", message="x")
		ref = error_ref(error_log)
		self.assertNotEqual(ref, error_log.name)
		self.assertNotIn("Error Log", ref)


class TestSelectButtonAuthorization(IntegrationTestCase):
	"""Integration tests for select_button security validations."""

	def setUp(self):
		# Create a test Stripe Settings (Gateway Controller) first
		if not frappe.db.exists("Stripe Settings", "_Test Controller"):
			self.controller = frappe.get_doc(
				{
					"doctype": "Stripe Settings",
					"gateway_name": "_Test Controller",
					"publishable_key": "pk_test_dummy",
					"secret_key": "sk_test_dummy",
				}
			)
			self.controller.flags.ignore_mandatory = True  # Skip API key validation
			self.controller.insert(ignore_permissions=True)

		# Create a test payment button (autoname from label)
		if not frappe.db.exists("Payment Button", "_Test PSL Button"):
			self.btn = frappe.get_doc(
				{
					"doctype": "Payment Button",
					"label": "_Test PSL Button",
					"enabled": 1,
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "_Test Controller",
				}
			)
			self.btn.insert(ignore_permissions=True)
		else:
			self.btn = frappe.get_doc("Payment Button", "_Test PSL Button")

		# Create a disabled button for testing
		if not frappe.db.exists("Payment Button", "_Test PSL Disabled"):
			self.disabled_btn = frappe.get_doc(
				{
					"doctype": "Payment Button",
					"label": "_Test PSL Disabled",
					"enabled": 0,
					"gateway_settings": "Stripe Settings",
					"gateway_controller": "_Test Controller",
				}
			)
			self.disabled_btn.insert(ignore_permissions=True)

	def _create_psl(self, status="Created", gateway=None):
		"""Helper to create a test PSL."""
		psl = frappe.get_doc(
			{
				"doctype": "Payment Session Log",
				"status": status,
				"tx_data": json.dumps({"amount": 100, "currency": "USD"}),
				"gateway": gateway,
			}
		)
		psl.insert(ignore_permissions=True)
		return psl

	def test_select_button_rejects_disabled_button(self):
		"""select_button should reject disabled buttons."""
		psl = self._create_psl()

		result = select_button(pslName=psl.name, buttonName="_Test PSL Disabled")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_rejects_terminal_psl(self):
		"""select_button should reject PSL in terminal state."""
		psl = self._create_psl(status="Paid")

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_rejects_mismatched_gateway(self):
		"""select_button should reject button that doesn't match PSL gateway filter."""
		# PSL requires a specific gateway
		gateway_filter = json.dumps(
			{
				"gateway_settings": "GoCardless Settings",
				"gateway_controller": "Different Controller",
			}
		)
		psl = self._create_psl(gateway=gateway_filter)

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNone(result)
		psl.reload()
		self.assertIsNone(psl.button)

	def test_select_button_accepts_valid_selection(self):
		"""select_button should accept valid button selection."""
		psl = self._create_psl()

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNotNone(result)
		self.assertTrue(result.get("reload"))
		psl.reload()
		self.assertEqual(psl.button, "_Test PSL Button")

	def test_select_button_accepts_matching_gateway(self):
		"""select_button should accept button matching PSL gateway filter."""
		gateway_filter = json.dumps(
			{
				"gateway_settings": "Stripe Settings",
				"gateway_controller": "_Test Controller",
			}
		)
		psl = self._create_psl(gateway=gateway_filter)

		result = select_button(pslName=psl.name, buttonName="_Test PSL Button")

		self.assertIsNotNone(result)
		psl.reload()
		self.assertEqual(psl.button, "_Test PSL Button")


class TestPaymentSessionLogStatusDefault(IntegrationTestCase):
	"""The schema default for `status` must match the code's initial state."""

	def test_create_log_starts_in_created(self):
		"""create_log() inserts a PSL in the 'Created' state."""
		psl = create_log(tx_data=_make_tx_data())
		self.assertEqual(psl.status, "Created")

	def test_bare_insert_defaults_to_created(self):
		"""A PSL inserted without an explicit status falls back to the schema
		default, which must be 'Created' (a state the state machine knows),
		not the unrecognized 'Queued'."""
		psl = frappe.get_doc(
			{
				"doctype": "Payment Session Log",
				"tx_data": json.dumps({"amount": 100, "currency": "USD"}),
			}
		)
		psl.insert(ignore_permissions=True)
		self.assertEqual(psl.status, "Created")

	def test_created_is_a_recognized_non_terminal_state(self):
		"""'Created' must be a valid, non-terminal state the machine accepts."""
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = "Created"
		self.assertFalse(psl.is_terminal())


class TestGetControllerFreshness(IntegrationTestCase):
	"""get_controller() must return a fresh, uncached controller instance so
	that PaymentController.state never bleeds between resolutions.

	No Payment Demo Settings fixture is needed: it is a Single, so it always
	resolves through frappe.get_doc, and nothing here reads its fields.
	"""

	def _create_psl(self):
		from payments.types import GatewayRef

		psl = create_log(tx_data=_make_tx_data())
		psl.db_set(
			"gateway",
			GatewayRef("Payment Demo Settings", "Payment Demo Settings").to_json(),
		)
		return psl

	def test_get_controller_returns_distinct_instances(self):
		"""Two resolutions must not share the same (cached) object."""
		psl = self._create_psl()
		first = psl.get_controller()
		second = psl.get_controller()
		self.assertIsNot(first, second)

	def test_get_controller_state_is_fresh(self):
		"""A controller resolved from a fresh PSL load starts with empty state,
		and mutating one instance's state does not leak into the next."""
		psl = self._create_psl()
		first = psl.get_controller()
		self.assertEqual(first.state, {})
		first.state.leaked = "should-not-persist"
		second = psl.get_controller()
		self.assertEqual(second.state, {})


class TestUpdateTxDataValidation(IntegrationTestCase):
	"""update_tx_data() must validate the merged result against TxData so bad
	updates fail fast instead of silently corrupting the stored JSON."""

	def test_well_formed_update_round_trips(self):
		"""A valid update merges cleanly and load_state() reconstructs TxData."""
		psl = create_log(tx_data=_make_tx_data())
		psl.update_tx_data({"amount": 99.5}, "Started")
		psl.reload()
		self.assertEqual(psl.status, "Started")
		state = psl.load_state()
		self.assertEqual(state.tx_data.amount, 99.5)
		self.assertEqual(state.tx_data.currency, "EUR")

	def test_malformed_update_raises_typeerror(self):
		"""An update introducing an unknown field must raise TypeError at update
		time, not persist silently and break the next load_state()."""
		psl = create_log(tx_data=_make_tx_data())
		with self.assertRaises(TypeError):
			psl.update_tx_data({"not_a_real_field": "x"}, "Started")
		# Nothing corrupt was persisted: original state still loads.
		psl.reload()
		state = psl.load_state()
		self.assertEqual(state.tx_data.amount, 25.00)


class TestCreateLogPayerPIIMinimization(IntegrationTestCase):
	"""M2: create_log() must strip non-essential PII from payer_contact /
	payer_address (full as_dict() output) down to the documented allowlists
	before persisting them in the PSL."""

	def test_strips_non_allowlisted_payer_keys(self):
		tx_data = TxData(
			amount=25.00,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
			payer_contact={
				"full_name": "Jane Doe",
				"email_id": "jane@example.com",
				"phone": "123",
				"mobile_no": "456",
				# noise that must NOT be persisted
				"owner": "x@internal",
				"modified_by": "admin@internal",
				"creation": "2020-01-01",
				"secret_note": "y",
				# `email` is deliberately off the allowlist: nothing reads it, and a
				# PII-minimisation allowlist should not keep a second copy of the
				# payer's address in a blob a guest page renders. Without this key in
				# the input, re-adding "email" to the allowlist left every test green.
				"email": "jane@example.com",
			},
			payer_address={
				"address_line1": "1 Main St",
				"city": "Town",
				"country": "NL",
				"owner": "x@internal",
				"custom_internal": "z",
			},
			loyalty_points=None,
			discount_amount=None,
		)
		psl = create_log(tx_data=tx_data)
		psl.reload()
		stored = json.loads(psl.tx_data)

		contact = stored["payer_contact"]
		# allowlisted fields survive
		self.assertEqual(contact.get("full_name"), "Jane Doe")
		self.assertEqual(contact.get("email_id"), "jane@example.com")
		self.assertEqual(contact.get("phone"), "123")
		self.assertEqual(contact.get("mobile_no"), "456")
		# everything else is gone
		for stripped in ("owner", "modified_by", "creation", "secret_note", "email"):
			self.assertNotIn(stripped, contact)

		address = stored["payer_address"]
		self.assertEqual(address.get("address_line1"), "1 Main St")
		self.assertEqual(address.get("city"), "Town")
		self.assertEqual(address.get("country"), "NL")
		for stripped in ("owner", "custom_internal"):
			self.assertNotIn(stripped, address)

	def test_tolerates_empty_and_partial_payer_dicts(self):
		"""Missing keys / empty dicts must not raise."""
		tx_data = TxData(
			amount=10.00,
			currency="EUR",
			reference_doctype="User",
			reference_docname="Administrator",
			payer_contact={},
			payer_address={"city": "Town"},
			loyalty_points=None,
			discount_amount=None,
		)
		psl = create_log(tx_data=tx_data)
		psl.reload()
		stored = json.loads(psl.tx_data)
		self.assertEqual(stored["payer_contact"], {})
		self.assertEqual(stored["payer_address"], {"city": "Town"})


class TestStatePredicates(unittest.TestCase):
	"""The PSL answers three independent questions about a status, and they do
	not agree — which is the whole point.

	- is_terminal():   should /pay stop showing the flow and show a result?
	- is_settled():    can no gateway callback change this outcome again?
	- is_disposable(): may the log be deleted past the retention window?

	Collapsing any two of these has already produced a defect: purging a
	Processing session destroyed the trail of a payment in flight, and treating
	Declined as settled dropped a late settlement. Pin all three per status here,
	in one table, so the next person can see the model rather than infer it.
	"""

	# status: (terminal, settled, disposable)
	EXPECTED: ClassVar[dict[str, tuple[bool, bool, bool]]] = {
		# never sent to a gateway, so there is no money record to protect
		"Created": (False, False, True),
		# "Started" is set immediately before the gateway call: a process killed
		# mid-call may have left a charge whose only trace is this row
		"Started": (False, False, False),
		"Initiated": (False, False, False),
		"Data Capture": (False, False, False),
		# the gateway still owes an answer: keep accepting callbacks, keep the log
		"Processing": (True, False, False),
		"Authorized": (True, False, False),
		# answered, and cannot change
		"Paid": (True, True, True),
		"Cancelled": (True, True, True),
		# answered, but a late callback is still accepted — and still purgeable
		"Declined": (True, False, True),
		"Error": (True, False, True),
		# The gateway answered and we could not act on its answer: terminal for the
		# page, NOT settled (a later mappable callback must still resolve it), and
		# NOT disposable, because money may be held and this log is the only trace.
		"Unresolved": (True, False, False),
	}

	def _psl(self, status, reconciliation=None):
		psl = PaymentSessionLog.__new__(PaymentSessionLog)
		psl.status = status
		psl.reconciliation = reconciliation
		return psl

	def test_every_status_answers_all_three_questions_as_declared(self):
		for status, (terminal, settled, disposable) in self.EXPECTED.items():
			with self.subTest(status=status):
				psl = self._psl(status)
				self.assertEqual(psl.is_terminal(), terminal, "is_terminal")
				self.assertEqual(psl.is_settled(), settled, "is_settled")
				self.assertEqual(psl.is_disposable(), disposable, "is_disposable")

	def test_the_table_covers_every_status_the_code_knows(self):
		"""The table is only a forcing function if adding a status breaks it.

		It did not: EXPECTED was iterated over itself, so a new status could be
		added to TERMINAL_STATES and the four sets without this test noticing —
		which is exactly what happened when "Unresolved" was introduced. Drive the
		comparison from the CODE's vocabulary instead.
		"""
		known = (
			set(PaymentSessionLog.TERMINAL_STATES)
			| PaymentSessionLog.SETTLED_STATES
			| PaymentSessionLog.DISPOSABLE_STATES
			| PaymentSessionLog.ABANDONED_STATES
			| PaymentSessionLog.RETRYABLE_STATES
		)
		self.assertEqual(
			known - set(self.EXPECTED),
			set(),
			"a status the code knows about is not declared in the table above",
		)

	def test_an_unreconciled_payment_is_never_disposable(self):
		"""A captured payment whose bookkeeping failed is the audit trail we most
		need to keep, however old and however settled the gateway side is."""
		self.assertTrue(self._psl("Paid").is_disposable())
		self.assertFalse(self._psl("Paid", reconciliation="Failed").is_disposable())

	def test_unfinished_bookkeeping_is_never_disposable(self):
		""" "Pending" is committed before the hook runs, so it is what a worker
		killed mid-hook leaves behind — a captured payment with bookkeeping that
		never finished. Purging that is the same data loss as purging a Failed one."""
		self.assertFalse(self._psl("Paid", reconciliation="Pending").is_disposable())
		self.assertTrue(self._psl("Paid", reconciliation="Done").is_disposable())

	def test_may_retry_charge_only_where_the_gateway_was_never_reached(self):
		"""The money question, kept apart from the display question: a Paid session
		is display-terminal but must never be re-initiated.

		Only `Error` qualifies, because it is the one state written before the
		gateway was reached. `Declined` is the interesting exclusion: it is
		equally "finished without money", but a decline is a gateway ANSWER, and
		this model deliberately lets a declined session still settle late — so a
		second charge on the same log could leave two live ones. A retry after a
		decline is a new session.
		"""
		self.assertTrue(self._psl("Error").may_retry_charge())
		for status in (
			"Declined",
			"Paid",
			"Authorized",
			"Processing",
			"Cancelled",
			"Unresolved",
			"Initiated",
			"Data Capture",
		):
			with self.subTest(status=status):
				self.assertFalse(self._psl(status).may_retry_charge())

	def test_error_refdoc_is_no_longer_a_gateway_outcome(self):
		"""Reconciliation failure lives in its own field now; it must not appear
		as a status, or it would overwrite what the gateway did."""
		self.assertNotIn("Error - RefDoc", PaymentSessionLog.TERMINAL_STATES)
		self.assertNotIn("Error - RefDoc", PaymentSessionLog.SETTLED_STATES)
		self.assertNotIn("Error - RefDoc", PaymentSessionLog.DISPOSABLE_STATES)


class TestClearOldLogs(IntegrationTestCase):
	"""clear_old_logs() must purge ALL terminal-state logs past the retention
	window, not just 'Paid' ones (else failed/errored logs grow unbounded)."""

	def _create_terminal_log(self, status, *, old):
		"""Create a terminal-status PSL; if old, backdate its modified column
		past the 90-day retention window via a direct, unmodified-tracking write."""
		psl = create_log(tx_data=_make_tx_data(), status=status)
		if old:
			# Bypass Frappe's modified-stamping by writing the column directly.
			frappe.db.set_value(
				"Payment Session Log",
				psl.name,
				"modified",
				"2000-01-01 00:00:00",
				update_modified=False,
			)
		return psl.name

	def test_clears_old_abandoned_sessions_but_keeps_anything_in_flight(self):
		"""Nothing purged the pre-terminal states, so a "Created" session lived
		forever — and payment_webform.accept is allow_guest and creates one per
		submission, so an anonymous caller could grow a money-audit table without
		bound.

		"Created" is the only safe addition: the session was opened and nothing was
		ever sent to a gateway. "Started" is set immediately BEFORE the gateway
		call, so a process killed mid-call may have left a real charge whose only
		trace is that row; Initiated and Data Capture have a live interaction. Those
		three stay.
		"""
		old_created = self._create_terminal_log("Created", old=True)
		old_started = self._create_terminal_log("Started", old=True)
		old_initiated = self._create_terminal_log("Initiated", old=True)
		old_capture = self._create_terminal_log("Data Capture", old=True)
		recent_created = self._create_terminal_log("Created", old=False)

		PaymentSessionLog.clear_old_logs(days=90)

		self.assertFalse(
			frappe.db.exists("Payment Session Log", old_created),
			"an abandoned session was retained forever",
		)
		self.assertTrue(frappe.db.exists("Payment Session Log", recent_created))
		for name, status in (
			(old_started, "Started"),
			(old_initiated, "Initiated"),
			(old_capture, "Data Capture"),
		):
			self.assertTrue(
				frappe.db.exists("Payment Session Log", name),
				f"a {status} session may have money behind it and was purged",
			)

	def test_clears_old_terminal_logs_keeps_recent(self):
		old_declined = self._create_terminal_log("Declined", old=True)
		old_error = self._create_terminal_log("Error", old=True)
		old_paid = self._create_terminal_log("Paid", old=True)
		recent_declined = self._create_terminal_log("Declined", old=False)

		PaymentSessionLog.clear_old_logs(days=90)

		self.assertFalse(frappe.db.exists("Payment Session Log", old_declined))
		self.assertFalse(frappe.db.exists("Payment Session Log", old_error))
		self.assertFalse(frappe.db.exists("Payment Session Log", old_paid))
		self.assertTrue(frappe.db.exists("Payment Session Log", recent_declined))

	def test_keeps_old_logs_whose_outcome_can_still_change(self):
		"""Processing/Authorized sessions are still in flight: the gateway owes us a
		final callback. Purging them destroys the audit trail of a payment that may
		yet settle, so the retention sweep must key on FINAL states, not on the
		display-terminal set."""
		old_processing = self._create_terminal_log("Processing", old=True)
		old_authorized = self._create_terminal_log("Authorized", old=True)
		old_paid = self._create_terminal_log("Paid", old=True)

		PaymentSessionLog.clear_old_logs(days=90)

		self.assertTrue(
			frappe.db.exists("Payment Session Log", old_processing),
			"a Processing log was purged while the payment was still in flight",
		)
		self.assertTrue(
			frappe.db.exists("Payment Session Log", old_authorized),
			"an Authorized log was purged before capture",
		)
		# Control: the sweep must still be doing its job.
		self.assertFalse(frappe.db.exists("Payment Session Log", old_paid))


class TestUpdateTxDataMinimizesPayerPII(IntegrationTestCase):
	"""The payer allowlist has to hold on every write, not just at create_log.

	payer_contact and payer_address are in UPDATABLE_TX_DATA_FIELDS, so a payer
	calling proceed() with updates reaches update_tx_data directly. If the
	projection lives only in create_log, the documented guarantee is false from
	the moment the payer proceeds.
	"""

	def test_update_projects_payer_contact_to_the_allowlist(self):
		psl = create_log(tx_data=_make_tx_data())
		psl.update_tx_data(
			{
				"payer_contact": {
					"full_name": "Jane Doe",
					"email_id": "jane@example.com",
					"owner": "internal@example.com",
					"secret_note": "KEEPME",
					"creation": "2020-01-01",
				}
			},
			"Started",
		)
		stored = json.loads(frappe.db.get_value("Payment Session Log", psl.name, "tx_data"))
		self.assertEqual(
			stored["payer_contact"],
			{"full_name": "Jane Doe", "email_id": "jane@example.com"},
			"non-allowlisted payer keys were persisted through update_tx_data",
		)

	def test_update_projects_payer_address_to_the_allowlist(self):
		psl = create_log(tx_data=_make_tx_data())
		psl.update_tx_data(
			{"payer_address": {"city": "Amsterdam", "owner": "internal@example.com"}},
			"Started",
		)
		stored = json.loads(frappe.db.get_value("Payment Session Log", psl.name, "tx_data"))
		self.assertEqual(stored["payer_address"], {"city": "Amsterdam"})

	def test_update_leaves_other_fields_untouched(self):
		"""Control: the projection must not eat unrelated updatable fields."""
		psl = create_log(tx_data=_make_tx_data())
		psl.update_tx_data({"payer_contact": {"full_name": "Jane"}}, "Started")
		stored = json.loads(frappe.db.get_value("Payment Session Log", psl.name, "tx_data"))
		expected = _make_tx_data()
		self.assertEqual(stored["amount"], expected.amount)
		self.assertEqual(stored["currency"], expected.currency)


class TestRetentionHandlesSqlNulls(IntegrationTestCase):
	"""is_disposable() is duplicated in Python and in clear_old_logs' SQL, and the
	two disagreed about NULL: `NULL != 'Failed'` is NULL in MariaDB, not TRUE, so
	a row whose reconciliation is SQL NULL — any row predating the column — was
	silently excluded from the purge while Python said it was disposable.
	"""

	def test_a_null_reconciliation_is_treated_as_not_failed(self):
		psl = create_log(tx_data=_make_tx_data(), status="Paid")
		frappe.db.sql(
			"UPDATE `tabPayment Session Log` SET reconciliation = NULL, modified = %s WHERE name = %s",
			("2000-01-01 00:00:00", psl.name),
		)
		self.assertIsNone(frappe.db.get_value("Payment Session Log", psl.name, "reconciliation"))
		self.assertTrue(frappe.get_doc("Payment Session Log", psl.name).is_disposable())

		PaymentSessionLog.clear_old_logs(days=90)

		self.assertFalse(
			frappe.db.exists("Payment Session Log", psl.name),
			"the SQL sweep and is_disposable() disagree about NULL",
		)

	def test_a_null_reconciliation_on_an_in_flight_session_is_still_kept(self):
		"""Control: NULL handling must not start purging in-flight sessions."""
		psl = create_log(tx_data=_make_tx_data(), status="Processing")
		frappe.db.sql(
			"UPDATE `tabPayment Session Log` SET reconciliation = NULL, modified = %s WHERE name = %s",
			("2000-01-01 00:00:00", psl.name),
		)
		PaymentSessionLog.clear_old_logs(days=90)
		self.assertTrue(frappe.db.exists("Payment Session Log", psl.name))


class TestRetentionIsActuallyWired(unittest.TestCase):
	"""clear_old_logs() is only reachable if the doctype is registered with
	frappe's log retention. Log Settings calls the doctype's own clear_old_logs,
	so registration is what makes OUR disposability rule run — and it is also
	what makes the window site-configurable, which the code comment claims.
	"""

	def test_payment_session_log_is_registered_for_log_clearing(self):
		registered = frappe.get_hooks("default_log_clearing_doctypes", {})
		self.assertIn("Payment Session Log", registered)

	def test_clear_old_logs_accepts_the_days_argument_log_settings_passes(self):
		import inspect

		sig = inspect.signature(PaymentSessionLog.clear_old_logs)
		self.assertIn("days", sig.parameters)


class TestSelectButtonFailsClosed(IntegrationTestCase):
	"""A filter whose failure mode is "no filter" is not a filter. A corrupt
	gateway value used to skip the gateway restriction entirely and accept any
	enabled button."""

	BUTTON = "_Test FailClosed Button"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.delete_doc("Payment Button", cls.BUTTON, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Payment Button",
				"label": cls.BUTTON,
				"enabled": 1,
				"gateway_settings": "Payment Demo Settings",
				"gateway_controller": "Payment Demo Settings",
				"implementation_variant": "Third Party Widget",
			}
		).insert(ignore_permissions=True)

	def test_a_corrupt_gateway_filter_refuses_the_selection(self):
		psl = create_log(tx_data=_make_tx_data())
		psl.db_set("gateway", "{not json at all", commit=True)

		select_button(pslName=psl.name, buttonName=self.BUTTON)

		self.assertIsNone(
			frappe.db.get_value("Payment Session Log", psl.name, "button"),
			"a corrupt gateway filter let a button through",
		)

	def test_get_controller_reports_an_unreadable_restriction(self):
		"""The third parse site. select_button failed closed and pay.py 500'd, and
		get_controller raised a raw JSONDecodeError out of the money path — the
		same corrupt value, three different outcomes. It must fail closed with a
		message a caller can act on."""
		psl = create_log(tx_data=_make_tx_data())
		psl.db_set("gateway", "{not json at all", commit=True)
		psl.reload()

		with self.assertRaises(frappe.ValidationError):
			psl.get_controller()


class TestAutoGeneratedTypesMatchTheSchema(IntegrationTestCase):
	"""The "auto-generated types" block must be what the generator would emit.

	It said "This code is auto-generated. Do not modify anything in this block."
	while missing reconciliation_error_message — a field the code reads — and
	holding the rest out of the generator's order. hooks.py sets
	export_python_type_annotations, so the next developer-mode save regenerates
	the block and produces a diff nobody intended.

	Asserted against frappe's own TypeExporter rather than a hand-rolled field
	filter: the earlier version of this test re-implemented the exclusion list
	(Section Break, Column Break, ...) and so was a second, laxer copy of
	frappe.model.no_value_fields — a `Heading` field would have failed it
	spuriously. Ask the generator, not a replica of it.
	"""

	DOCTYPES = ("Payment Session Log", "Payment Button")

	def test_the_block_is_what_the_generator_would_emit(self):
		import re

		from frappe.types.exporter import TypeExporter

		for doctype in self.DOCTYPES:
			exporter = TypeExporter(frappe.get_doc("DocType", doctype))
			expected = exporter._generate_code()
			actual = exporter.controller_path.read_text()

			first_line, *_, last_line = expected.splitlines()
			self.assertIn(first_line, actual, f"{doctype}: no auto-generated block at all")
			block = actual[actual.index(first_line) : actual.index(last_line) + len(last_line)]

			# Compare the annotations, not the whitespace: the generator re-derives
			# indentation from the file it is writing into.
			def annotations(text):
				return re.findall(r"^\s*(\w+): (DF\.[^\n]+)$", text, re.M)

			self.assertEqual(
				annotations(block),
				annotations(expected),
				f"{doctype}: the block drifted from what the generator emits",
			)


class TestSelectButtonClientMessages(IntegrationTestCase):
	"""Refusal messages must use frappe's message_log shape.

	frappe.msgprint appends a DICT (frappe/utils/messages.py:117), and the client
	JSON-decodes each entry — so a plain string arrives as '"text"' and renders to
	the payer with literal quotation marks around it. Worse, anything later in the
	request that walks message_log calls .get() on the entry
	(frappe/utils/response.py:82) and would AttributeError on a str.
	"""

	def test_a_refusal_message_is_a_dict_with_a_message_key(self):
		saved = frappe.local.message_log
		frappe.local.message_log = []
		self.addCleanup(lambda: setattr(frappe.local, "message_log", saved))

		select_button(pslName="no-such-session", buttonName="no-such-button")

		self.assertTrue(frappe.local.message_log, "the payer was told nothing")
		entry = frappe.local.message_log[0]
		self.assertIsInstance(entry, dict, f"message_log carried a bare {type(entry).__name__}")
		self.assertTrue(entry.get("message"))


class TestRetentionSqlMatchesThePredicate(IntegrationTestCase):
	"""is_disposable() and clear_old_logs are twins: the predicate has NO
	production consumer (the sweep re-expresses it in SQL), so the only thing
	keeping them in step is this test.

	They have diverged before — `NULL != 'Failed'` is NULL in MariaDB, not TRUE,
	so every row predating the reconciliation column was excluded from the sweep
	while Python said it was disposable. Compare them across every combination
	rather than per status, because that divergence lived in the interaction.
	"""

	RECONCILIATIONS = (None, "", "Pending", "Done", "Failed")

	def test_the_sweep_deletes_exactly_what_the_predicate_permits(self):
		statuses = sorted(
			# TERMINAL_STATES is the display + colour MAP, not a set.
			set(PaymentSessionLog.TERMINAL_STATES)
			| PaymentSessionLog.DISPOSABLE_STATES
			| PaymentSessionLog.ABANDONED_STATES
			| PaymentSessionLog.SETTLED_STATES
			| PaymentSessionLog.RETRYABLE_STATES
			| {"Created", "Started", "Initiated", "Data Capture"}
		)

		expected = {}
		for status in statuses:
			for rec in self.RECONCILIATIONS:
				psl = create_log(tx_data=_make_tx_data(), status=status)
				frappe.db.set_value(
					"Payment Session Log", psl.name, "reconciliation", rec, update_modified=False
				)
				frappe.db.set_value(
					"Payment Session Log",
					psl.name,
					"modified",
					"2000-01-01 00:00:00",
					update_modified=False,
				)
				psl.reload()
				expected[psl.name] = (status, rec, psl.is_disposable())

		PaymentSessionLog.clear_old_logs(days=90)

		disagreements = []
		for name, (status, rec, predicate_says_disposable) in expected.items():
			sql_deleted_it = not frappe.db.exists("Payment Session Log", name)
			if sql_deleted_it != predicate_says_disposable:
				disagreements.append(
					f"{status!r}/{rec!r}: is_disposable()={predicate_says_disposable} "
					f"but SQL deleted={sql_deleted_it}"
				)

		self.assertEqual(len(expected), len(statuses) * len(self.RECONCILIATIONS))
		self.assertEqual(disagreements, [], "the predicate and its SQL twin disagree")


class TestSelectButtonDiagnosticsNameTheSession(IntegrationTestCase):
	"""Every refusal that knows which session it is refusing must say so.

	Five of the six branches used frappe.log_error(<f-string>, reference_doctype=...),
	which sets the doctype and leaves reference_name empty — so an operator reading
	the Error Log could not tell which payment the refusal belonged to. Same defect,
	and same fix, as the one applied to the HTTPError arm of _run_initiation thirty
	lines away: Document.log_error links both. The positional shape also puts the
	dynamic cause in the 140-char `method` field instead of the message body.
	"""

	BUTTON = "_Test Diagnostics Button"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.delete_doc("Payment Button", cls.BUTTON, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Payment Button",
				"label": cls.BUTTON,
				"enabled": 1,
				"gateway_settings": "Payment Demo Settings",
				"gateway_controller": "Payment Demo Settings",
				"implementation_variant": "Third Party Widget",
			}
		).insert(ignore_permissions=True)

	def _latest_log(self):
		return frappe.get_all(
			"Error Log",
			fields=["method", "error", "reference_doctype", "reference_name"],
			order_by="creation desc",
			limit=1,
		)[0]

	def test_a_terminal_session_refusal_names_the_session(self):
		psl = create_log(tx_data=_make_tx_data(), status="Paid")
		select_button(pslName=psl.name, buttonName=self.BUTTON)
		log = self._latest_log()
		self.assertEqual(log.reference_name, psl.name, "the refusal did not name the session")
		self.assertEqual(log.reference_doctype, "Payment Session Log")

	def test_a_gateway_mismatch_refusal_names_the_session(self):
		psl = create_log(tx_data=_make_tx_data())
		psl.db_set("gateway", '{"gateway_settings": "Something Else"}', commit=True)
		select_button(pslName=psl.name, buttonName=self.BUTTON)
		log = self._latest_log()
		self.assertEqual(log.reference_name, psl.name, "the refusal did not name the session")
		# The dynamic cause belongs in the message, not truncated into `method`.
		self.assertIn("Something Else", log.error)


class TestSelectButtonAfterInitiation(IntegrationTestCase):
	"""Once the gateway has been called, the payer may not switch method.

	select_button only refused terminal sessions, and `Initiated` is not
	terminal — so an unauthenticated holder of the session URL could rewrite
	`button` and `selected_gateway` while an initiation was already recorded.
	Nothing binds the stored idempotency token to the button it was made for, so
	the next /pay render replays the first gateway's payload into the second
	gateway's widget. `Initiated` is neither terminal nor retryable, so no new
	charge is ever started either: the payer is stuck.

	`Declined` stays retryable, so choosing another method after a decline — the
	case the chooser exists for — is unaffected.
	"""

	BUTTON_A = "_Test Switch A"
	BUTTON_B = "_Test Switch B"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		for label in (cls.BUTTON_A, cls.BUTTON_B):
			frappe.delete_doc("Payment Button", label, force=True, ignore_permissions=True)
			frappe.get_doc(
				{
					"doctype": "Payment Button",
					"label": label,
					"enabled": 1,
					"gateway_settings": "Payment Demo Settings",
					"gateway_controller": "Payment Demo Settings",
					"implementation_variant": "Third Party Widget",
				}
			).insert(ignore_permissions=True)

	def test_a_selection_cannot_change_once_the_gateway_has_been_called(self):
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.BUTTON_A, commit=True)
		PaymentController.proceed(psl.name)
		psl.reload()
		self.assertEqual(psl.status, "Initiated")
		self.assertTrue(psl.initiation_response_payload, "precondition: an initiation is recorded")

		select_button(pslName=psl.name, buttonName=self.BUTTON_B)

		self.assertEqual(
			frappe.db.get_value("Payment Session Log", psl.name, "button"),
			self.BUTTON_A,
			"the method was switched out from under a live initiation",
		)
		self.assertTrue(frappe.local.message_log, "the payer was told nothing")

	def test_a_session_that_has_not_been_initiated_may_still_choose(self):
		"""The control: the guard must only bite once an initiation exists.

		Note what is NOT the control here. A declined session cannot re-select
		either, but that is the pre-existing terminal check, not this guard —
		`Declined` is display-terminal, which is the documented gap about retry
		from `/pay`. An earlier version of this test asserted a declined session
		could switch method, which was never true.
		"""
		psl = create_log(tx_data=_make_tx_data(), controller=frappe.get_doc("Payment Demo Settings"))
		psl.db_set("button", self.BUTTON_A, commit=True)

		select_button(pslName=psl.name, buttonName=self.BUTTON_B)

		self.assertEqual(
			frappe.db.get_value("Payment Session Log", psl.name, "button"),
			self.BUTTON_B,
			"the guard blocked a selection before any initiation existed",
		)


class TestSelectButtonRateLimit(IntegrationTestCase):
	"""select_button is allow_guest and five of its six refusal branches insert an
	Error Log row, so an anonymous caller could grow the table without bound —
	measured at one row per call. The sibling guest endpoint get_checkout_url was
	rate limited for exactly this reason, in the commit that named the cost;
	this one was not. Two guest endpoints, one limit.
	"""

	LIMIT = 20  # keep in step with the decorator on select_button

	def test_an_anonymous_caller_is_rate_limited(self):
		"""Exercises the limiter for real: frappe's rate_limit short-circuits when
		there is no request, so a test that just calls the function proves nothing.
		A unique IP per run keeps the redis bucket from leaking between runs.
		"""
		import uuid

		saved_request, saved_ip = (
			getattr(frappe.local, "request", None),
			getattr(frappe.local, "request_ip", None),
		)
		frappe.local.request = frappe._dict(method="POST")
		frappe.local.request_ip = f"203.0.113.{uuid.uuid4().int % 250}"
		frappe.form_dict.cmd = f"select_button_probe_{uuid.uuid4().hex[:8]}"

		def _restore():
			frappe.local.request = saved_request
			frappe.local.request_ip = saved_ip
			frappe.form_dict.pop("cmd", None)

		self.addCleanup(_restore)

		# Assert the LIMIT, not merely that one exists: a 40-call loop wrapped in
		# assertRaises passes just as well with limit=1, which would refuse a
		# payer's second method switch inside a minute. Mutation confirmed that —
		# limit=20 -> limit=1 survived the earlier version of this test. The number
		# is the safety property, so the number is what gets pinned.
		allowed = 0
		with self.assertRaises(frappe.RateLimitExceededError):
			for _ in range(self.LIMIT + 5):
				select_button(pslName="no-such-session", buttonName="no-such-button")
				allowed += 1
		self.assertEqual(allowed, self.LIMIT, f"{allowed} calls were allowed, not {self.LIMIT}")


class TestGatewayRestrictionSurvivesSelection(IntegrationTestCase):
	"""`gateway` answers two different questions, and one writer clobbers the other.

	create_log writes it as the INITIATOR's restriction ("this session must use
	gateway X", or NULL for no restriction). select_button then overwrote it with
	the PAYER's selection. Three consumers read it: the /pay chooser filter, the
	select_button authorization filter, and get_controller().

	So the moment a payer picks anything, an unrestricted session becomes
	restricted to that gateway: "or change payment method" can only ever offer the
	button already chosen, and a retry after a decline is pinned to the gateway
	that just declined it.
	"""

	BUTTON = "_Test Restriction Button"

	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.delete_doc("Payment Button", cls.BUTTON, force=True, ignore_permissions=True)
		frappe.get_doc(
			{
				"doctype": "Payment Button",
				"label": cls.BUTTON,
				"enabled": 1,
				"gateway_settings": "Payment Demo Settings",
				"gateway_controller": "Payment Demo Settings",
				"implementation_variant": "Third Party Widget",
			}
		).insert(ignore_permissions=True)

	def test_selection_does_not_narrow_an_unrestricted_session(self):
		psl = create_log(tx_data=_make_tx_data())
		self.assertFalse(psl.gateway, "an unrestricted session should start with no gateway")

		select_button(pslName=psl.name, buttonName=self.BUTTON)
		psl.reload()

		self.assertFalse(
			psl.gateway,
			"the payer's selection overwrote the initiator's restriction, so the chooser "
			"can now only offer the button already chosen",
		)
		self.assertTrue(psl.selected_gateway, "the selection was not recorded anywhere")
		self.assertEqual(psl.button, self.BUTTON)

	def test_the_controller_resolves_from_the_selection(self):
		"""Control: whichever field holds it, the flow must still find its controller."""
		psl = create_log(tx_data=_make_tx_data())
		select_button(pslName=psl.name, buttonName=self.BUTTON)
		psl.reload()
		self.assertIsNotNone(psl.get_controller())

	def test_an_initiator_restriction_is_preserved(self):
		"""Control: a session created against a specific gateway stays restricted."""
		demo = frappe.get_doc("Payment Demo Settings")
		psl = create_log(tx_data=_make_tx_data(), controller=demo)
		restriction = psl.gateway
		self.assertTrue(restriction)

		select_button(pslName=psl.name, buttonName=self.BUTTON)
		psl.reload()
		self.assertEqual(psl.gateway, restriction)
