# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import dataclasses
import json
from typing import TYPE_CHECKING, ClassVar, TypedDict

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import IfNull, Now
from frappe.rate_limiter import rate_limit

from payments.types import (
	GatewayProcessingResponse,
	GatewayRef,
	Initiated,
	RemoteServerInitiationPayload,
	TxData,
)
from payments.utils import error_ref

if TYPE_CHECKING:
	from payments.controllers import PaymentController
	from payments.payments.doctype.payment_button.payment_button import PaymentButton


class PSLState(TypedDict):
	"""State returned by PaymentSessionLog.load_state()"""

	psl: dict
	tx_data: TxData


class PaymentSessionLog(Document):
	# TODO: Remove vestigial `mandate` field from payment_session_log.json
	# The mandate system was removed from PaymentController but the DocType field
	# remains to avoid a schema migration. Clean up when convenient.

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		button: DF.Data | None
		correlation_id: DF.Data | None
		data_capture_payload: DF.Code | None
		decline_reason: DF.Data | None
		flow_type: DF.Data | None
		gateway: DF.Data | None
		initiation_response_payload: DF.Code | None
		mandate: DF.Data | None
		processing_response_payload: DF.Code | None
		reconciliation: DF.Literal["", "Pending", "Done", "Failed"]
		reconciliation_error: DF.Link | None
		reconciliation_error_message: DF.Code | None
		selected_gateway: DF.Data | None
		status: DF.Data | None
		title: DF.Data | None
		tx_data: DF.Code | None
	# end: auto-generated types

	# TERMINAL_STATES answers a DISPLAY question: "should /pay stop rendering the
	# payment flow and show a result instead?" It doubles as the indicator colour
	# map. Processing and Authorized belong here — there is nothing further for the
	# payer to do on the page.
	TERMINAL_STATES: ClassVar[dict[str, str]] = {
		"Paid": "green",
		"Authorized": "green",
		"Processing": "yellow",
		"Declined": "red",
		"Cancelled": "red",
		"Error": "red",
		# The gateway answered and we could not act on its answer — its response
		# failed to process, or it reported a status this controller does not map.
		# Distinct from "Error", which means initiation failed before anything was
		# sent: there, nothing was charged and a retry is free. Here money may be
		# held, so this state is deliberately NOT retryable and NOT disposable, and
		# it needs an operator.
		"Unresolved": "red",
	}

	# SETTLED_STATES answers: "can no gateway callback ever change this outcome
	# again?" It gates re-processing, and nothing else.
	#
	# Deliberately absent: Processing and Authorized, because the gateway still
	# owes a settlement or decline (the Processing action label is literally
	# "Refresh"); Unresolved, because the whole point of that state is that a
	# later, mappable callback should still resolve it; Error, because a webhook
	# retry should be able to succeed; and
	# Declined, because whether a PSP can settle a session it already declined is
	# not something we know for every gateway, and dropping such a callback loses
	# money.
	SETTLED_STATES: ClassVar[frozenset[str]] = frozenset({"Paid", "Cancelled"})

	# DISPOSABLE_STATES answers a third, independent question: "may this log be
	# deleted once past the retention window?" It is neither of the sets above.
	#
	# Not TERMINAL_STATES: Processing and Authorized are terminal for the page but
	# the gateway still owes an answer, so deleting them destroys the trail of a
	# payment in flight. Not SETTLED_STATES either: Declined and Error accept a
	# late callback yet must still be purgeable, or declined sessions accumulate
	# forever — which is precisely what the retention sweep exists to prevent.
	# Keeping retention separate is what lets both hold at once.
	# Unresolved is absent on purpose: the gateway answered, we could not act on
	# it, and money may be held — that log is the only trace of it.
	DISPOSABLE_STATES: ClassVar[frozenset[str]] = frozenset({"Paid", "Declined", "Cancelled", "Error"})

	def is_terminal(self) -> bool:
		"""Whether /pay should stop showing the flow and show a result instead.

		NOT the same as is_settled() or is_disposable(): a Processing or Authorized
		session is finished as far as the page is concerned, but its outcome can
		still change and its log must be retained.
		"""
		return self.status in self.TERMINAL_STATES

	# RETRYABLE_STATES answers a fourth question, and it is the one the money path
	# needs: "may a NEW charge be started for this session?" It is true only where
	# the session finished WITHOUT the gateway holding money, so another attempt is
	# genuinely wanted — which is also what the declined path's `button = None`
	# reset serves. That retry is reachable server-side only (a fresh proceed()
	# from the reference document): /pay stops at is_terminal(), and Declined is
	# terminal, so the payer sees a result rather than the chooser.
	#
	# It is emphatically not is_terminal(): a Paid session is display-terminal, so
	# gating initiation on that predicate fell straight through into a second
	# gateway charge, overwrote Paid with Initiated, and nulled the processing
	# payload. Four questions, four sets; do not reuse one for another's job.
	#
	# The line is whether a charge attempt exists at the GATEWAY. "Error" is
	# written only where initiation failed before the gateway was reached, so
	# there is nothing to collide with and re-using the session is safe.
	#
	# "Declined" is deliberately NOT here, though it is equally "finished without
	# money". A decline is a gateway answer, so an attempt does exist — and this
	# model keeps Declined out of SETTLED_STATES precisely because a PSP that
	# later settles a session it declined must not be ignored. Both hold at once
	# only if no second charge is started on that session, so a retry after a
	# decline is a NEW session, initiated by the reference document. In-place
	# retry would leave two live charges behind one log, with record_initiation
	# overwriting the first one's correlation id.
	#
	# A failure while PROCESSING a gateway response is the same story once more:
	# the gateway has spoken and may hold money, so that writes "Unresolved".
	RETRYABLE_STATES: ClassVar[frozenset[str]] = frozenset({"Error"})

	def may_retry_charge(self) -> bool:
		"""Whether a new charge may be started for this session."""
		return self.status in self.RETRYABLE_STATES

	def has_an_unrecorded_attempt(self) -> bool:
		"""Whether a PREVIOUS request reached the gateway and never recorded a result.

		"Started" is written immediately before the gateway call and by nothing
		else, so finding a session at "Started" with no initiation payload at the
		START of a new proceed() is the fingerprint of a request that died in
		between — a SIGKILL, a worker timeout, an evicted container. It cannot be
		an in-process failure: every one of those paths writes "Error" (the gateway
		was never reached) or "Unresolved" (it was). A session that was never
		attempted is "Created".

		Deliberately conservative. A request that died between writing "Started"
		and actually calling the gateway is indistinguishable from one that died
		just after, so this reports True for both. On a money path the false
		positive costs an operator a look; the false negative charges the payer
		twice. The durable fix is to ASK the gateway whether a charge exists for
		this session, which is a per-gateway contract and not implemented here.
		"""
		return self.status == "Started" and not self.initiation_response_payload

	def is_settled(self) -> bool:
		"""Whether the gateway outcome is settled and no callback can change it."""
		return self.status in self.SETTLED_STATES

	# A session can also become disposable from the other end: it was opened and
	# nothing was ever sent to a gateway, so there is no money record to protect.
	# Only "Created" qualifies. "Started" is set immediately BEFORE the gateway
	# call, so a process killed mid-call may have left a real charge whose only
	# trace is that row; "Initiated" and "Data Capture" have a live interaction.
	# Without this, nothing purged the pre-terminal states at all — and
	# payment_webform.accept is allow_guest and creates a "Created" session per
	# submission, so an anonymous caller could grow a money-audit table forever.
	ABANDONED_STATES: ClassVar[frozenset[str]] = frozenset({"Created"})

	def is_disposable(self) -> bool:
		"""Whether this log may be deleted once past the retention window.

		Either the gateway gave a final answer (DISPOSABLE_STATES) or the session
		never reached a gateway at all (ABANDONED_STATES) — AND our own bookkeeping
		must not have failed or be unfinished, since an unreconciled captured
		payment is exactly the audit trail to keep, however old it is, and
		"Pending" means a worker was killed between committing Pending and writing
		the outcome.

		clear_old_logs expresses this same condition in SQL; keep the two in step.
		"""
		return self.status in (
			self.DISPOSABLE_STATES | self.ABANDONED_STATES
		) and self.reconciliation not in (
			"Failed",
			"Pending",
		)

	def get_indicator_color(self) -> str:
		"""Get the indicator color for the current status."""
		return self.TERMINAL_STATES.get(self.status, "gray")

	def update_tx_data(self, tx_data: dict, status: str | None = None) -> None:
		"""Merge updates into tx_data, and optionally move the status.

		`status` is optional because the two are separate decisions. Writing
		"Started" here — before the caller's pre-flight work — made a pre-gateway
		failure indistinguishable from a charge interrupted mid-flight, since both
		leave "Started" with no payload. See has_an_unrecorded_attempt().
		"""
		# tx_data is a dict of updates (the controller passes
		# _filter_tx_data_updates(...) output). Reconstruct a TxData from the
		# merged result before persisting so a type-mismatched update raises
		# TypeError here instead of silently corrupting the stored JSON and
		# blowing up later in load_state() (TxData(**json.loads(...))).
		merged = {**json.loads(self.tx_data), **tx_data}
		# Apply the payer allowlist here, at the PSL boundary, and not only in
		# create_log: payer_contact and payer_address are in
		# UPDATABLE_TX_DATA_FIELDS, so a payer calling proceed() with updates
		# reaches this method directly. Projecting only on create left the
		# documented minimisation guarantee false from the first payer update.
		merged = _minimize_payer_pii(merged)
		validated = TxData(**merged)
		updates = {"tx_data": frappe.as_json(dataclasses.asdict(validated))}
		if status is not None:
			updates["status"] = status
		self.db_set(updates, commit=True)

	def update_gateway_specific_state(self, data: dict, status: str) -> None:
		"""Store gateway-specific state fetched during the data capture phase.

		Deliberately NOT sharing a writer with set_initiation_payload. The two look
		alike — both persist a JSON blob plus a status — but the values mean
		different things: initiation_response_payload is the gateway's response to
		our initiation and is what proceed() treats as its idempotency token, while
		this is scratch state for the capture form. Writing one through the other
		destroyed the token and let a page reload charge the payer twice.
		"""
		self.db_set(
			{
				"data_capture_payload": frappe.as_json(data),
				"status": status,
			},
			commit=True,
		)

	def set_initiation_payload(self, initiation_payload: RemoteServerInitiationPayload, status: str) -> None:
		"""Persist the gateway's initiation response — proceed()'s idempotency token."""
		self.db_set(
			{
				"initiation_response_payload": frappe.as_json(initiation_payload),
				"status": status,
			},
			commit=True,
		)

	def record_initiation(self, initiated: "Initiated", flow_type: str) -> None:
		"""Persist EVERYTHING a successful initiation produced, in one commit.

		This used to be two commits — the gateway metadata first, then the payload
		and status — and the gap between them is the window in which the session
		has been charged but holds no idempotency token. Another request landing
		there sees a half-written session, and if the second commit never happened
		the next proceed() would charge again. One write, one commit, so the
		session either records the whole initiation or none of it.
		"""
		self.db_set(
			{
				"processing_response_payload": None,  # in case of a reset
				"flow_type": flow_type,
				"correlation_id": initiated.correlation_id,
				"initiation_response_payload": frappe.as_json(initiated.payload),
				"status": "Initiated",
			},
			commit=True,
		)

	def set_processing_payload(self, processing_response: GatewayProcessingResponse, status: str) -> None:
		self.db_set(
			{
				"processing_response_payload": frappe.as_json(processing_response.payload),
				"status": status,
			},
			commit=True,
		)

	def load_state(self):
		return frappe._dict(
			psl=frappe._dict(self.as_dict()),
			tx_data=TxData(**json.loads(self.tx_data)),
		)

	def parse_gateway_ref(self, raw: str | None) -> dict | None:
		"""Parse a stored gateway restriction. None means UNREADABLE.

		Three sites parse this value — the /pay chooser filter, select_button's
		authorization check, and get_controller — and they disagreed on what a
		corrupt value means: select_button failed closed, pay.py raised
		JSONDecodeError and served the payer a 500, and get_controller raised it
		onto the money path. A filter whose failure mode is "no filter" is not a
		filter, so every caller must read None as "no payment method available".

		An empty dict means "no restriction", which is a different and legitimate
		answer.
		"""
		if not raw:
			return {}
		try:
			parsed = json.loads(raw)
		except (json.JSONDecodeError, TypeError):
			parsed = None
		# The KEYS matter as much as the type. /pay does filters.update(restriction)
		# on a get_all filter dict, so an unexpected key — `enabled` above all —
		# would let a restriction WIDEN the chooser and put disabled buttons in
		# front of the payer. A restriction may only ever narrow, so accept exactly
		# GatewayRef's fields and nothing else. get_controller already got this
		# right via GatewayRef(**parsed); the other two call sites did not.
		allowed = {f.name for f in dataclasses.fields(GatewayRef)}
		if not isinstance(parsed, dict) or not set(parsed) <= allowed:
			self.log_error(
				title="Unreadable gateway filter on payment session",
				message=f"psl={self.name} gateway={raw!r}",
			)
			return None
		return parsed

	def gateway_filter(self) -> dict | None:
		"""The initiator's restriction on which buttons may be used."""
		return self.parse_gateway_ref(self.gateway)

	def get_controller(self) -> "PaymentController":
		"""For perfomance reasons, this is not implemented as a dynamic link but a json value
		so that it is only fetched when absolutely necessary.
		"""
		# The payer's selection wins; the initiator's restriction is the fallback
		# for a session that was created against a specific gateway and never went
		# through the chooser.
		ref_json = self.selected_gateway or self.gateway
		if not ref_json:
			self.log_error("No gateway selected yet")
			frappe.throw(_("No gateway selected for this payment session"))
		# None (unreadable) and {} (no restriction) are different answers, and
		# conflating them is exactly what parse_gateway_ref's docstring forbids:
		# `gateway = "{}"` reported "unreadable" when the truth is that nothing was
		# selected. Say the accurate thing, since it is what an operator acts on.
		parsed = self.parse_gateway_ref(ref_json)
		if parsed is None:
			frappe.throw(_("The gateway on this payment session is unreadable"))
		if not parsed:
			self.log_error("No gateway selected yet")
			frappe.throw(_("No gateway selected for this payment session"))
		try:
			ref = GatewayRef(**parsed)
		except TypeError:
			# Keys are already checked against GatewayRef's fields; this catches a
			# partial ref (one field missing).
			frappe.throw(_("The gateway on this payment session is unreadable"))
		# Use get_doc (NOT get_cached_doc) so each resolution yields a fresh
		# controller instance with a fresh `self.state`. A cached controller
		# reused within one request (e.g. a webhook processing several events
		# for the same gateway) would otherwise carry stale state between calls.
		return frappe.get_doc(ref.gateway_settings, ref.gateway_controller)

	def get_button(self) -> "PaymentButton":
		if not self.button:
			self.log_error("No button selected yet")
			frappe.throw(_("No button selected for this payment session"))
		return frappe.get_cached_doc("Payment Button", self.button)

	@staticmethod
	def clear_old_logs(days=90):
		# The SQL form of is_disposable(): the gateway must have given a final
		# answer OR never have been contacted at all, so Processing and Authorized
		# are never purged while it still owes one and neither are Started /
		# Initiated / Data Capture, which may have money behind them; AND
		# reconciliation must not have failed, so a captured payment whose
		# bookkeeping failed keeps its audit trail however old it is.
		# The default here is only a default: Log Settings passes its own `days`
		# (LogSettings.clear_logs builds kwargs via frappe.get_newargs), so the
		# window IS site-configurable already — hooks.py says so, and this comment
		# used to claim the opposite.
		table = frappe.qb.DocType("Payment Session Log")
		disposable = list(PaymentSessionLog.DISPOSABLE_STATES | PaymentSessionLog.ABANDONED_STATES)
		frappe.db.delete(
			table,
			filters=(table.modified < (Now() - Interval(days=days)))
			& (table.status.isin(disposable))
			# IfNull, because `NULL != 'Failed'` is NULL in MariaDB, not TRUE — so a
			# row whose reconciliation is SQL NULL (any row predating the column)
			# was silently excluded here while Python's is_disposable() said True.
			& (IfNull(table.reconciliation, "").notin(["Failed", "Pending"])),
		)


# CSRF (M3): Frappe skips CSRF validation for guest sessions, so this guest
# endpoint could otherwise be driven by a cross-origin POST. Restricting it to
# POST blocks the trivial GET/<img>/link vectors and simple cross-origin form
# submits that don't already know the ~35-bit PSL name. Residual risk: a fully
# scripted cross-origin POST (fetch/XHR) is still possible if the attacker
# knows a valid, non-terminal PSL name; impact is bounded (it only switches
# among already-enabled buttons matching the PSL's gateway filter, never alters
# amount/refdoc). A per-session CSRF token issued in the /pay page context is
# the recommended follow-up; deliberately not built here to avoid half-baked
# token infra on this branch.
@frappe.whitelist(allow_guest=True, methods=["POST"])  # nosemgrep: guest-whitelisted-method
# Five of the six refusal branches below insert an Error Log row, so without a
# limit an anonymous caller grows that table one row per request. IP-only bucket:
# frappe derives it from ip:form_dict[key], so keying on anything the caller
# sends would hand it a fresh bucket per value it invents. 20/min is well above
# what a payer switching methods needs.
@rate_limit(limit=20, seconds=60)
def select_button(pslName: str | None = None, buttonName: str | None = None) -> str:
	"""Select a payment button for a payment session.

	Security validations:
	- Button must be enabled
	- Button must match PSL gateway filter (if set)
	- PSL must be in a pre-terminal state (not already paid/failed)
	"""
	try:
		psl = frappe.get_doc("Payment Session Log", pslName)
	except Exception:
		# The one refusal that genuinely cannot name a session: there is no document
		# to hang it on. Every other branch uses psl.log_error, which links
		# reference_doctype AND reference_name.
		e = frappe.log_error(
			title="Payment session not found", message=f"pslName={pslName!r} button={buttonName!r}"
		)
		# Return an opaque correlation code, not the Error Log docname: the name
		# embeds internal timestamp/naming. Support can still correlate via the
		# trailing chars stored in the (full) server-side Error Log. (M4)
		frappe.local.message_log = [{"message": _("Server Failure! Reference: {0}").format(error_ref(e))}]
		return

	# Validate PSL is in a state where button selection is allowed
	if psl.is_terminal():
		psl.log_error(
			title="Button selection on a terminal payment session",
			message=f"psl={pslName} status={psl.status} button={buttonName}",
		)
		frappe.local.message_log = [{"message": _("This payment session is no longer active.")}]
		return

	# An initiation already recorded pins the session to the button it was made
	# for: nothing binds the stored idempotency token to a button, so switching
	# now replays this gateway's payload into another gateway's widget — and
	# "Initiated" is neither terminal nor retryable, so no new charge is started
	# either and the payer is simply stuck. Same condition as proceed()'s
	# idempotency guard, so a retryable session (a decline) is unaffected by this
	# check.
	if psl.initiation_response_payload and not psl.may_retry_charge():
		psl.log_error(
			title="Selection change after the gateway was called",
			message=f"psl={psl.name} button={psl.button!r} requested={buttonName!r}",
		)
		frappe.local.message_log = [{"message": _("This payment is already in progress.")}]
		return

	try:
		btn: PaymentButton = frappe.get_cached_doc("Payment Button", buttonName)
	except Exception:
		e = psl.log_error(title="Payment button not found", message=f"psl={psl.name} button={buttonName}")
		# Return an opaque correlation code, not the Error Log docname (M4),
		# matching the PSL-not-found path above.
		frappe.local.message_log = [{"message": _("Server Failure! Reference: {0}").format(error_ref(e))}]
		return

	# Validate button is enabled
	if not btn.enabled:
		psl.log_error(
			title="Disabled payment button selected",
			message=f"psl={psl.name} button={buttonName}",
		)
		frappe.local.message_log = [{"message": _("This payment method is not available.")}]
		return

	# Validate button matches PSL gateway filter (if set)
	gateway_filter = psl.gateway_filter()
	if gateway_filter is None:
		# Fail CLOSED. This used to `pass`, so a corrupt gateway value skipped the
		# whole restriction and accepted any enabled button — the failure mode of a
		# filter should never be "no filter". The parser logs the corrupt value.
		frappe.local.message_log = [
			{"message": _("This payment method is not available for this transaction.")}
		]
		return
	if gateway_filter:
		# Check if selected button matches the required gateway settings/controller
		if (
			gateway_filter.get("gateway_settings")
			and gateway_filter["gateway_settings"] != btn.gateway_settings
		):
			psl.log_error(
				title="Button gateway mismatch",
				message=(
					f"psl={psl.name} button={buttonName} "
					f"expected={gateway_filter.get('gateway_settings')!r} got={btn.gateway_settings!r}"
				),
			)
			frappe.local.message_log = [
				{"message": _("This payment method is not available for this transaction.")}
			]
			return
		if (
			gateway_filter.get("gateway_controller")
			and gateway_filter["gateway_controller"] != btn.gateway_controller
		):
			psl.log_error(
				title="Button controller mismatch",
				message=(
					f"psl={psl.name} button={buttonName} "
					f"expected={gateway_filter.get('gateway_controller')!r} got={btn.gateway_controller!r}"
				),
			)
			frappe.local.message_log = [
				{"message": _("This payment method is not available for this transaction.")}
			]
			return

	# Write the payer's choice to selected_gateway, NOT to gateway. `gateway` is
	# the initiator's restriction, and three consumers read it: the /pay chooser
	# filter, the authorization filter above, and (as a fallback) get_controller.
	# Overwriting it turned an unrestricted session into one restricted to
	# whatever the payer first clicked, so "or change payment method" could only
	# ever offer that same button and a retry after a decline stayed pinned to the
	# gateway that declined it.
	psl.db_set(
		{
			"button": buttonName,
			"selected_gateway": GatewayRef(btn.gateway_settings, btn.gateway_controller).to_json(),
		}
	)
	# once state set: reload the page to activate widget
	return {"reload": True}


# Data minimization (M2): TxData.payer_contact / payer_address arrive as full
# document dicts (contact.as_dict() / address.as_dict()), which carry PII and
# bookkeeping fields we neither need nor want to persist for the PSL retention
# window or expose to guest-rendered templates (owner, modified_by, timestamps,
# custom fields, etc.). Project them down to the minimal fields actually used
# for the payment + display. Anything not on these allowlists is stripped.
# /pay renders payer.full_name, and the initiators that matter supply the parts
# rather than the whole, so first_name/last_name are accepted and full_name is
# derived from them below when absent.
#
# Scope of that claim, deliberately narrow: the ERPNext-side producer this tracks
# (a _get_contact_fields emitting a payer_contact dict) is NOT in the installed
# ERPNext v16.30.0 — grep finds zero occurrences of it or of payer_contact
# anywhere outside this repo, and ERPNext currently calls
# controller.get_payment_url(payer_name=..., payer_email=...), the v1 shape. So
# these entries are prospective, for the consumer-side branch that introduces
# them. `email` is deliberately NOT accepted: nothing in this repo reads
# payer_contact["email"], and a PII-minimisation allowlist should not store a
# second copy of the payer's address in a guest-rendered blob on an unverified
# claim.
_PAYER_CONTACT_ALLOWLIST = frozenset(
	{"full_name", "first_name", "last_name", "email_id", "phone", "mobile_no"}
)
_PAYER_ADDRESS_ALLOWLIST = frozenset(
	{"address_line1", "address_line2", "city", "state", "country", "pincode"}
)


def _project_allowed(value, allowlist: frozenset) -> dict:
	"""Return only the allowlisted keys of a dict; defensive against non-dicts
	and missing keys (tolerates partially-populated payer documents)."""
	if not isinstance(value, dict):
		return value
	return {k: value[k] for k in allowlist if k in value}


def _minimize_payer_pii(tx_data_dict: dict) -> dict:
	"""Strip non-essential PII from payer_contact/payer_address before persisting."""
	if "payer_contact" in tx_data_dict:
		tx_data_dict["payer_contact"] = _project_allowed(
			tx_data_dict["payer_contact"], _PAYER_CONTACT_ALLOWLIST
		)
		# /pay renders payer.full_name, and an initiator may supply only the parts
		# (see the allowlist note above: the ERPNext-side producer that would is
		# prospective, not merged). Derive it rather than showing the payer the
		# word "None" — though pay.html defaults it too, since the template is the
		# last line of defence and this fix only covers one producer.
		contact = tx_data_dict["payer_contact"]
		if isinstance(contact, dict) and not contact.get("full_name"):
			parts = [contact.get("first_name"), contact.get("last_name")]
			derived = " ".join(p for p in parts if p)
			if derived:
				contact["full_name"] = derived
	if "payer_address" in tx_data_dict:
		tx_data_dict["payer_address"] = _project_allowed(
			tx_data_dict["payer_address"], _PAYER_ADDRESS_ALLOWLIST
		)
	return tx_data_dict


def create_log(
	tx_data: TxData,
	controller: "PaymentController" = None,
	status: str = "Created",
) -> PaymentSessionLog:
	log = frappe.new_doc("Payment Session Log")
	# TxData is a dataclass — convert to dict for JSON serialization
	tx_data_dict = dataclasses.asdict(tx_data) if dataclasses.is_dataclass(tx_data) else tx_data
	tx_data_dict = _minimize_payer_pii(tx_data_dict)
	log.tx_data = frappe.as_json(tx_data_dict)
	log.status = status
	if controller:
		log.gateway = GatewayRef(controller.doctype, controller.name).to_json()

	log.insert(ignore_permissions=True)
	return log
