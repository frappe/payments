from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, NoReturn
from urllib.parse import quote, urlencode

import frappe
from frappe import _
from frappe.desk.form.load import get_document_email
from frappe.email.doctype.email_account.email_account import EmailAccount
from frappe.model.base_document import get_controller
from frappe.model.document import Document
from frappe.utils import get_url
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock
from requests.exceptions import HTTPError

from payments.exceptions import (
	FailedToInitiateFlowError,
	PayloadIntegrityError,
	PaymentControllerProcessingError,
	RefDocHookProcessingError,
)
from payments.payments.doctype.payment_session_log.payment_session_log import (
	PaymentSessionLog,
	create_log,
)
from payments.types import (
	ActionAfterProcessed,
	FrontendDefaults,
	GatewayProcessingResponse,
	Initiated,
	PaymentUrl,
	Proceeded,
	Processed,
	PSLName,
	RemoteServerInitiationPayload,
	SessionStates,
	SessionType,
	TxData,
	_Processed,
)
from payments.utils import PAYMENT_SESSION_REF_KEY, error_ref

if TYPE_CHECKING:
	from payments.payments.doctype.payment_gateway.payment_gateway import PaymentGateway


def _session_lock_name(psl_name: PSLName) -> str:
	"""The one lock guarding a payment session.

	proceed() and process_response() contend for the same resource, so they must
	take the same lock. Two different names are never mutually exclusive, which is
	what let a webhook settle a session in the window between /pay's terminal
	check and its call to proceed().
	"""
	return f"payment-session-{psl_name}"


def _error_value(error, flow):
	return _(
		"Our server had a problem processing your {0}. Please contact customer support mentioning: {1}"
	).format(flow, error_ref(error))


def _redirect_on_initiation_error(psl, error, *, include_psl: bool = False) -> NoReturn:
	"""Redirect the user to a generic payment-gateway error message and raise.

	``psl`` is only interpolated into the message when ``include_psl`` is True.
	The Error Log reference is shortened to an opaque code (the PSL name is
	already known to the user via the /pay URL, so only the Error Log ref needs
	hardening). Always raises ``frappe.Redirect`` — callers never resume.
	"""
	if include_psl:
		body = _("Please contact customer care mentioning: {0} and {1}").format(psl, error_ref(error))
	else:
		body = _("Please contact customer care mentioning: {0}").format(error_ref(error))
	frappe.redirect_to_message(
		_("Payment Gateway Error"),
		body,
		http_status_code=401,
		indicator_color="yellow",
	)
	raise frappe.Redirect


class PaymentController(Document):
	"""This controller implements the public API of payment gateway controllers."""

	if TYPE_CHECKING:
		frontend_defaults: FrontendDefaults
		flowstates: SessionStates

	# Fields that can be updated at proceed() time.
	# Critical fields (amount, currency, reference_doctype, reference_docname) are NOT allowed
	# to prevent tampering via the guest-facing /pay endpoint.
	UPDATABLE_TX_DATA_FIELDS = frozenset(
		{
			"payer_contact",
			"payer_address",
			"loyalty_points",
			"discount_amount",
		}
	)

	@staticmethod
	def _filter_tx_data_updates(updates: dict | None) -> dict:
		"""Filter updated_tx_data to only allow whitelisted fields.

		This prevents tampering with critical fields like amount, currency,
		and reference document through the proceed() endpoint.

		Args:
		        updates: The raw updates dict from the caller

		Returns:
		        Filtered dict containing only allowed fields
		"""
		if not updates:
			return {}

		filtered = {}
		rejected = []

		for key, value in updates.items():
			if key in PaymentController.UPDATABLE_TX_DATA_FIELDS:
				filtered[key] = value
			else:
				rejected.append(key)

		if rejected:
			# Not frappe.logger(): measured on test_site_2, frappe.logger("payments")
			# has an effective level of ERROR with file-only handlers and
			# propagate=False, so .warning() was dropped entirely and an attempted
			# tamper on a money path left no trace anywhere an operator would look.
			# proceed() is not whitelisted, so a guest cannot drive this to flood
			# the Error Log.
			frappe.log_error(
				title="Rejected non-whitelisted tx_data update",
				message=f"Rejected keys: {sorted(rejected)}",
			)

		return filtered

	def __init_subclass__(cls, **kwargs):
		# These are subclass-definition invariants (every concrete gateway must
		# declare flowstates/frontend_defaults as CLASS attributes), so validate
		# them once at class-definition (import) time rather than on every
		# instantiation. __init_subclass__ runs for subclasses only, so the
		# PaymentController base class itself — which legitimately doesn't declare
		# them — is never checked here.
		# NOTE: because this fires at class-definition time, an *abstract
		# intermediate* subclass (one that deliberately defers flowstates/
		# frontend_defaults to its own concrete subclasses) would raise here at
		# import. There are none today (gateways subclass PaymentController
		# directly). If one is introduced, guard this check (e.g. skip when the
		# class is marked abstract) rather than declaring placeholder attrs.
		super().__init_subclass__(**kwargs)
		if not (hasattr(cls, "flowstates") and isinstance(cls.flowstates, SessionStates)):
			raise TypeError(
				f"{cls.__name__} must declare cls.flowstates as an instance of payments.types.SessionStates"
			)
		if not (hasattr(cls, "frontend_defaults") and isinstance(cls.frontend_defaults, FrontendDefaults)):
			raise TypeError(
				f"{cls.__name__} must declare cls.frontend_defaults as an instance of payments.types.FrontendDefaults"
			)

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.state = frappe._dict()

	@staticmethod
	def initiate(
		tx_data: TxData,
		gateway: PaymentController | None = None,
		correlation_id: str | None = None,
		name: str | None = None,
	) -> tuple[PaymentController, PSLName]:
		"""Initiate a payment flow from Ref Doc with the given gateway.

		Inheriting methods can invoke super and then set e.g. correlation_id on self.state.psl to save
		and early-obtained correlation id from the payment gateway or to initiate the user flow if delegated to
		the controller (see: is_user_flow_initiation_delegated)
		"""
		if isinstance(gateway, str):
			payment_gateway: PaymentGateway = frappe.get_cached_doc("Payment Gateway", gateway)

			if not payment_gateway.gateway_controller and not payment_gateway.gateway_settings:
				frappe.throw(
					_(
						"{0} is not fully configured, both Gateway Settings and Gateway Controller need to be set"
					).format(gateway)
				)

			# Use get_doc (not get_cached_doc) for the controller: it carries mutable
			# per-flow `self.state`, so a cached instance could bleed state across
			# resolutions within one request. Mirrors PaymentSessionLog.get_controller.
			self = frappe.get_doc(
				payment_gateway.gateway_settings,
				payment_gateway.gateway_controller or payment_gateway.gateway_settings,  # may be a singleton
			)
		else:
			self = gateway

		self.validate_tx_data(tx_data)  # preflight check

		psl = create_log(
			tx_data=tx_data,
			controller=self,
			status="Created",
		)
		return self, psl.name

	@staticmethod
	def get_payment_url(psl_name: PSLName) -> PaymentUrl | None:
		"""Use the payment url to initiate the user flow, for example via email or chat message.

		Beware, that the controller might not implement this and in that case return: None
		"""
		params = {
			PAYMENT_SESSION_REF_KEY: psl_name,
		}
		return get_url(f"./pay?{urlencode(params)}")

	@staticmethod
	def pre_data_capture_hook(psl_name: PSLName) -> dict:
		"""Call this before presenting the user with a form to capture additional data.

		Implementation is optional, but can be used to acquire any additonal data from the remote
		gateway that should be present already during data capture.
		"""

		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", psl_name)
		self: PaymentController = psl.get_controller()
		data = self._pre_data_capture_hook()
		psl.update_gateway_specific_state(data, "Data Capture")
		return data

	@staticmethod
	def proceed(psl_name: PSLName, updated_tx_data: TxData = None) -> Proceeded:
		"""Call this when the user agreed to proceed with the payment to initiate the capture with
		the remote payment gateway.

		If the capture is initialized by the gatway, call this immediatly without waiting for the
		user OK signal.

		updated_tx_data:
		   Pass any update to the inital transaction data; this can reflect later customer choices
		   and thereby modify the flow. Only whitelisted fields can be updated (see
		   UPDATABLE_TX_DATA_FIELDS). Critical fields like amount, currency, and reference
		   document cannot be changed to prevent tampering.

		Example:
		```python
		if controller.is_user_flow_initiation_delegated():
		    controller.proceed()
		else:
		    # example (depending on the doctype & business flow):
		    # 1. send email with payment link
		    # 2. let user open the link
		    # 3. upon rendering of the page: call proceed; potentially with tx updates
		    pass
		```
		"""

		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", psl_name)
		self: PaymentController = psl.get_controller()

		# Serialise initiation for this session. Document.lock is unusable here:
		# frappe's file_lock docstring states it is "not suitable for
		# synchroniztion". A DB row lock does not help either, because
		# update_tx_data commits before _initiate_charge and would release the lock
		# before the very call it needs to guard. synchronization.filelock is the
		# primitive frappe designates for process synchronisation and is not
		# transaction-scoped.
		#
		# Limitation: the lock file lives under the site directory, so this
		# serialises processes on one host, not across app servers or containers.
		# The durable fix is a gateway-side idempotency key — a per-gateway
		# contract, since each PSP names it differently.
		try:
			with filelock(_session_lock_name(psl_name), timeout=10):
				psl.reload()
				return self._run_initiation(psl, updated_tx_data)
		except LockTimeoutError:
			error = psl.log_error(title="Timed out waiting to initiate this payment session")
			_redirect_on_initiation_error(psl, error, include_psl=True)

	def _run_initiation(self, psl: PaymentSessionLog, updated_tx_data: TxData = None) -> Proceeded:
		"""The body of proceed(), run while holding the session lock.

		The idempotency guard is deliberately re-read here rather than in
		proceed(): a caller that waited on the lock must see whatever the winner
		of the race committed and return it, instead of initiating a second
		charge against the gateway.
		"""

		# Idempotency: if we already hold a gateway initiation, return it instead of
		# initiating again — unless the session finished WITHOUT the gateway holding
		# money, where a fresh attempt is genuinely wanted (may_retry_charge()).
		#
		# Not is_terminal(): that is the display question, and a Paid session is
		# display-terminal, so gating here on it fell through into a second real
		# gateway charge. Not is_settled() either, which would let Processing and
		# Authorized re-charge while the bank still owes an answer.
		if psl.initiation_response_payload and not psl.may_retry_charge():
			self._load_and_patch(psl)
			payload = json.loads(psl.initiation_response_payload)
			return Proceeded(
				integration=self.doctype,
				psltype=psl.flow_type or SessionType.charge,
				txdata=self.state.tx_data,
				payload=payload,
			)

		# The guard above can only refuse what it can replay: it needs a stored
		# payload. A session that reached the gateway but never recorded the result
		# has none — status "Unresolved" — and so would fall straight through to a
		# second charge. State the rule the payload check was standing in for: a
		# session that is finished as far as the page is concerned, and not marked
		# retryable, must never start a new charge. That also covers Processing and
		# Authorized, where the bank still owes an answer.
		if psl.is_terminal() and not psl.may_retry_charge():
			psl.log_error(
				title="Refused to initiate a charge on a finished session",
				message=f"psl={psl.name} status={psl.status}",
			)
			frappe.throw(
				_("This payment session is no longer active."),
				title=_("Cannot start a new payment"),
			)

		# The two guards above both need something recorded to refuse: a stored
		# payload, or a status the failure paths wrote. A request that simply STOPPED
		# between the gateway accepting the charge and record_initiation committing
		# it — killed, timed out, evicted — leaves neither, only "Started". That is
		# non-terminal and has no token, so it fell through both and charged again.
		if psl.has_an_unrecorded_attempt():
			psl.log_error(
				title="Refused to re-charge an interrupted attempt",
				message=(
					f"psl={psl.name} was left at {psl.status!r} with no initiation payload, so a "
					"previous request reached the gateway and never recorded the result. "
					"Reconcile against the gateway before retrying."
				),
			)
			psl.db_set("status", "Unresolved", commit=True)
			frappe.throw(
				_("This payment needs to be checked before it can be tried again."),
				title=_("Cannot start a new payment"),
			)

		# Filter updates to only allow whitelisted fields (security: prevents tampering)
		filtered_updates = PaymentController._filter_tx_data_updates(updated_tx_data)
		# No status change here. "Started" is the marker for "we are about to call
		# the gateway", and writing it before the pre-flight work below made a
		# pre-gateway failure — a schema-drift error in load_state, or a controller
		# deliberately redirecting out of _patch_tx_data — leave exactly the state
		# an interrupted charge leaves. has_an_unrecorded_attempt() then refused the
		# next attempt and blocked the payment for good, for a charge that was never
		# made.
		psl.update_tx_data(filtered_updates)  # commits

		self._load_and_patch(psl)

		try:
			frappe.flags.integration_request_doc = psl  # for linking error logs

			# Immediately before the call, and nowhere else: this is what makes
			# "Started" mean "the gateway may have been reached".
			#
			# Clearing the payload is part of that meaning, not housekeeping. The
			# two gateway-failure arms below STORE one
			# (set_initiation_payload(err.data, "Error")), so a retry after such a
			# failure would otherwise run with a superseded attempt's payload
			# attached — which makes has_an_unrecorded_attempt() blind to an
			# interruption here, and leaves the idempotency guard ready to replay a
			# dead error blob as though it were an initiation response.
			psl.db_set({"status": "Started", "initiation_response_payload": None}, commit=True)
			initiated = self._initiate_charge()

		# some gateways don't return HTTP errors ...
		except FailedToInitiateFlowError as err:
			psl.set_initiation_payload(err.data, "Error")
			error = psl.log_error(title=err.message)
			_redirect_on_initiation_error(psl, error, include_psl=True)

		# ... yet others do ...
		except HTTPError as err:
			# v2 sets frappe.flags.integration_request_doc (the PSL), never the v1
			# frappe.flags.integration_request, so reading the latter raised
			# AttributeError and masked the original HTTPError. Read the response
			# body off the exception itself, with a safe fallback.
			try:
				data = err.response.json() if err.response is not None else {}
			except ValueError:
				data = {"error": str(err)}
			psl.set_initiation_payload(data, "Error")
			# Record our own diagnostic. This used to be
			# frappe.get_last_doc("Error Log"), which handed error_ref the most
			# recent row in the WHOLE table — so the code the payer was told to
			# quote named an unrelated failure — and which raises
			# DoesNotExistError on an empty table, from inside this except, so a
			# fresh site 500'd instead of redirecting.
			error = psl.log_error(
				title="Gateway HTTP error during initiation", message=frappe.get_traceback()
			)
			_redirect_on_initiation_error(psl, error, include_psl=True)

		except Exception:
			# Write the outcome, exactly as the two arms above do. Leaving the
			# status alone left the session at "Started" — which is not
			# may_retry_charge() — while the previous attempt's payload survived,
			# so every later proceed() returned that dead payload and made no
			# gateway call: the payer could never pay. Reachable from an ordinary
			# network blip, since ConnectionError and Timeout are not HTTPError.
			# Log BEFORE the write: if what we just caught was itself a database
			# failure, db_set raises and the diagnostic would be lost with it.
			error = psl.log_error(title="Unknown Initialization Failure", message=frappe.get_traceback())
			psl.db_set({"status": "Error", "initiation_response_payload": None}, commit=True)
			# include_psl, like the other four call sites. This arm is the one an
			# ordinary network blip reaches, so it is the likeliest to reach a payer,
			# and its support message was the only one that did not name the session.
			_redirect_on_initiation_error(psl, error, include_psl=True)

		# Deliberately outside the try above, whose three arms all mean "the gateway
		# call failed". By here it has SUCCEEDED, and failing to record that is a
		# different thing: the payer has been charged. Writing "Error" for it — which
		# the catch-all above would have done — puts the session in RETRYABLE_STATES
		# with no idempotency token, so the next proceed() charges again.
		try:
			psl.record_initiation(initiated, SessionType.charge)
		except Exception:
			# The Error Log is now the only record that this charge happened, so it
			# carries the payload and the correlation id for manual reconciliation.
			error = psl.log_error(
				title="Charge initiated but not recorded",
				message=(
					f"correlation_id={initiated.correlation_id!r}\n"
					f"payload={initiated.payload!r}\n\n{frappe.get_traceback()}"
				),
			)
			try:
				psl.db_set("status", "Unresolved", commit=True)
			except Exception:
				# The database is what failed; the Error Log above is durable anyway.
				pass
			_redirect_on_initiation_error(psl, error, include_psl=True)

		return Proceeded(
			integration=self.doctype,
			psltype=SessionType.charge,
			txdata=self.state.tx_data,
			payload=initiated.payload,
		)

	def _load_and_patch(self, psl: PaymentSessionLog) -> None:
		"""Rebuild `self.state` from the stored session and apply the gateway's own
		preprocessing.

		Both of _run_initiation's paths need this, and both need the same
		handling: rebuilding TxData from the stored JSON can fail on schema drift
		(a key an older release wrote), and without a handler the exception escapes
		proceed() and leaves the session with no diagnostic. The replay path is if
		anything the more likely one to hit drift, being the one that reads a
		session an older release wrote — it had no handler while the fresh path
		did, which is the whole reason this is one function.

		frappe.Redirect passes through untouched: a controller may redirect out of
		_patch_tx_data, and that is control flow rather than failure.
		"""
		try:
			self.state = psl.load_state()
			self.state.tx_data = self._patch_tx_data(self.state.tx_data)
		except frappe.Redirect:
			raise
		except Exception:
			error = psl.log_error(
				title="Could not load payment session state", message=frappe.get_traceback()
			)
			_redirect_on_initiation_error(psl, error, include_psl=True)

	def get_frontend_safe_context(self) -> dict:
		"""Fields safe to expose to the guest /pay templates. Override per gateway
		to expose ONLY non-secret values (e.g. a publishable key). Default: none.

		Security: the full gateway settings document holds API secrets (secret_key,
		webhook secrets, tokens). It must NEVER be handed to templates whose rendered
		output is injected into the public /pay page — a single `{{ doc.secret_key }}`
		in any gateway template would leak credentials to every visitor. Callers build
		the template context from this projection instead of the raw doc.
		"""
		return {}

	def _get_support_email(self):
		"""Look up the support email for the reference document, falling back to default incoming."""
		incoming = get_document_email(
			self.state.tx_data.reference_doctype,
			self.state.tx_data.reference_docname,
		)
		if not incoming:
			account = EmailAccount.find_default_incoming()
			incoming = account.email_id if account else None
		return incoming

	def _build_support_action(self, psl, subject, body, fallback_action):
		"""Build a mailto action for user support, falling back to fallback_action if no email configured."""
		incoming_email = self._get_support_email()
		if incoming_email:
			params = {
				"subject": subject,
				"body": body,
			}
			href = f"mailto:{incoming_email}?{urlencode(params, quote_via=quote)}"
			return dict(href=href, label=_("Email Us"))
		return fallback_action

	def _build_compensatory_action(self, psl, error_log):
		return self._build_support_action(
			psl,
			subject=_("Payment Server Error: {}").format(error_log),
			# nosemgrep: frappe-translation-python-splitting - newlines in email body are intentional
			body=_("Reference:\n\n- PSL: {}\n- Error Log: {}\n- RefDoc: {}\n\nThank you!").format(
				frappe.utils.get_url_to_form("Payment Session Log", psl.name),
				frappe.utils.get_url_to_form("Error Log", error_log.name),
				frappe.utils.get_url_to_form(
					self.state.tx_data.reference_doctype, self.state.tx_data.reference_docname
				),
			),
			fallback_action=dict(href="/", label=_("Go to Homepage")),
		)

	# Status category → (psl_status, indicator_color, message_template, action_label)
	# Note: action labels are raw strings; wrapped in _() at render time to support i18n.
	# Translation markers for extraction: _("Go to Homepage"), _("Refresh")
	# Message markers: _("{} succeeded"), _("{} authorized"), _("{} awaiting further processing by the bank")
	_STATUS_MAP: ClassVar[dict] = {
		"success": ("Paid", "green", "{} succeeded", dict(href="/", label="Go to Homepage")),
		"pre_authorized": ("Authorized", "green", "{} authorized", dict(href="/", label="Go to Homepage")),
		"processing": (
			"Processing",
			"yellow",
			"{} awaiting further processing by the bank",
			dict(href="/", label="Refresh"),
		),
	}

	def _process_response(self, psl: PaymentSessionLog, ref_doc: Document) -> Processed:
		self._validate_response()

		processed = None
		try:
			processed = self._process_response_for_charge()  # idempotent on second run
		except Exception as e:
			raise PaymentControllerProcessingError(
				f"{self._process_response_for_charge} failed", "charge"
			) from e

		all_states = (
			self.flowstates.success
			+ self.flowstates.pre_authorized
			+ self.flowstates.processing
			+ self.flowstates.declined
		)
		if self.flags.status_changed_to not in all_states:
			# An unmapped status must surface as a handled error: process_response's
			# outer try only catches PaymentControllerProcessingError (and siblings),
			# not a bare ValueError. Raising ValueError here would escape with a
			# traceback and return nothing to the frontend; raise the controller
			# error so the existing error path renders a clean red Processed.
			raise PaymentControllerProcessingError(
				f"Gateway returned an unmapped status: {self.flags.status_changed_to}", "charge"
			)

		ret = {
			"status_changed_to": self.flags.status_changed_to,
			"payload": self.state.response.payload,
		}

		changed = False

		# Handle success / pre_authorized / processing (common structure)
		for category, (psl_status, color, msg_template, action_label) in self._STATUS_MAP.items():
			if self.flags.status_changed_to in getattr(self.flowstates, category):
				changed = psl_status != psl.status
				psl.db_set("decline_reason", None)
				psl.set_processing_payload(self.state.response, psl_status)  # commits
				ret["indicator_color"] = color
				processed = processed or Processed(
					message=_(msg_template).format("charge".title()),
					action=dict(action_label, label=_(action_label["label"])),
					**ret,
				)
				break

		# Handle declined (structurally different: resets button, builds support mailto)
		if self.flags.status_changed_to in self.flowstates.declined:
			changed = "Declined" != psl.status
			psl.db_set(
				{
					"decline_reason": self._render_failure_message(),
					# The flow is over for this session: a retry after a decline is
					# a NEW session (see RETRYABLE_STATES), because a declined
					# charge may still settle and a second charge on the same log
					# would leave two live ones. The selection is cleared so the
					# record does not suggest a method that is no longer in play.
					"button": None,
				}
			)
			psl.set_processing_payload(self.state.response, "Declined")  # commits
			ret["indicator_color"] = "red"

			action = self._build_support_action(
				psl,
				subject=_("Help! Payment declined: {}, {}").format(
					self.state.tx_data.reference_docname, psl.name
				),
				# nosemgrep: frappe-translation-python-splitting - newlines in email body are intentional
				body=_("Please help me with:\n- PSL: {}\n- RefDoc: {}\n\nThank you!").format(
					frappe.utils.get_url_to_form("Payment Session Log", psl.name),
					frappe.utils.get_url_to_form(
						self.state.tx_data.reference_doctype, self.state.tx_data.reference_docname
					),
				),
				# Not a link back to /pay labelled "Refresh": Declined is
				# display-terminal, so that page shows the same result again and
				# does not even render decline_reason — an invitation to a
				# pointless click on the one page where a payment just failed.
				fallback_action=dict(href="/", label=_("Go to Homepage")),
			)
			processed = processed or Processed(
				message=_("{} declined").format("charge".title()),
				action=action,
				**ret,
			)

		return self._invoke_ref_doc_hook(psl, ref_doc, changed, ret, processed)

	def _invoke_ref_doc_hook(
		self, psl: PaymentSessionLog, ref_doc: Document, changed: bool, ret: dict, processed: Processed
	) -> Processed:
		"""Invoke the optional ``on_payment_charge_processed`` hook on the ref doc.

		The hook is optional (for smoother adoption); when present it may override
		the default ``processed`` value built by ``_process_response``. Any failure
		in user/server-script code is wrapped in ``RefDocHookProcessingError`` after
		scrubbing the client-visible message log, so no internal details leak.

		Returns the (possibly overridden) ``Processed``.
		"""
		hookmethod = "on_payment_charge_processed"
		has_hook = hasattr(ref_doc, hookmethod) and callable(getattr(ref_doc, hookmethod, None))

		if not has_hook:
			# Nothing to reconcile: leave reconciliation blank rather than claiming
			# "Done", so blank keeps meaning "not applicable".
			return processed

		psl.db_set("reconciliation", "Pending", commit=True)

		try:
			ref_doc.flags.payment_session = frappe._dict(
				changed=changed, state=self.state, flags=self.flags, flowstates=self.flowstates
			)  # when run as server script: can only set flags
			res = ref_doc.run_method(
				hookmethod,
				changed,
				self.state,
				self.flags,
				self.flowstates,
			)
			# result from server script run
			res = ref_doc.flags.payment_result or res
			if res:
				# type check the result value on user implementations
				res["action"] = ActionAfterProcessed(**res.get("action", {})).__dict__
				_res = _Processed(**res)
				processed = Processed(**(ret | _res.__dict__))
			psl.db_set("reconciliation", "Done", commit=True)
		except Exception as e:
			# Replace whatever the hook left in the message log, so a RefDoc hook
			# cannot leak its own internals to the client. `body` below is str(e):
			# process_response has no allow_guest caller on this branch, so this is a
			# desk-side surface. If it ever becomes guest-reachable, body must go and
			# be replaced by error_ref().
			frappe.local.message_log = [
				{
					"message": _("Server Processing Failure!"),
					"subtitle": _("(during RefDoc processing)"),
					"body": str(e),
					"indicator": "red",
				}
			]
			raise RefDocHookProcessingError("RefDoc hook processing failed", "charge") from e

		return processed

	@staticmethod
	def process_response(psl_name: PSLName, response: GatewayProcessingResponse) -> Processed:
		"""Call this from the controlling business logic; either backend or frontend.

		It will recover the correct controller and dispatch the correct processing based on data that is at this
		point already stored in the integration log

		payload:
		    this is a signed, sensitive response containing the payment status; the signature is validated prior
		    to processing by controller._validate_response
		"""

		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", psl_name)
		self: PaymentController = psl.get_controller()

		# Guard against concurrent processing (webhook + client confirm racing, or two
		# webhook deliveries). This takes the SAME lock as proceed(), and takes it
		# through synchronization.filelock rather than Document.lock: the latter is
		# frappe's "weak" file_lock, whose acquire is a check followed by a
		# non-atomic touch, so two deliveries arriving together could both pass it
		# and both run the reference document's bookkeeping hook — two Payment
		# Entries for one payment.
		try:
			lock = filelock(_session_lock_name(psl_name), timeout=10)
			lock.__enter__()
		except LockTimeoutError:
			# We waited and the holder is still working (or died holding it). Record
			# it and re-raise rather than returning a state-report Processed.
			#
			# Returning one was the old behaviour and it is wrong on the path that
			# matters: a webhook caller reads a non-error return as accepted, answers
			# 200, and the gateway stops resending — so if the holder then fails, the
			# event is lost. Re-raising gives the gateway its retry, which is exactly
			# the right response to "busy". The cost is a 500 for an interactive
			# refresh landing inside a 10s window, which is rare and recoverable.
			psl.log_error(title="Timed out waiting for the payment session lock")
			raise

		try:
			psl.reload()

			# After acquiring the lock, check whether this session is already settled.
			# Gate on is_settled(), NOT is_terminal(): Processing and Authorized are
			# display-terminal but still awaiting a gateway callback, and skipping that
			# callback would strand the payment in Processing permanently.
			if psl.is_settled():
				return Processed(
					message=_(psl.status),
					action=dict(href="/", label=_("Go to Homepage")),
					status_changed_to=psl.status,
					indicator_color=psl.get_indicator_color(),
					payload={},
				)

			self.state = psl.load_state()
			self.state.response = response

			ref_doc = frappe.get_doc(
				self.state.tx_data.reference_doctype,
				self.state.tx_data.reference_docname,
			)

			try:
				mute = self._is_server_to_server()
			except Exception:
				# A gateway's own flow detection must not be able to abort processing.
				# Fail to the interactive path: it returns a message to the caller
				# rather than swallowing the outcome, which is the safer default.
				psl.log_error(title="Determining server-to-server flow failed")
				mute = False

			def make_error_processed(error, message):
				return Processed(
					message=message,
					action=self._build_compensatory_action(psl, error),
					status_changed_to=_("Server Error"),
					indicator_color="red",
					payload={},
				)

			try:
				processed = self._process_response(psl, ref_doc)
				if self.flags.status_changed_to in self.flowstates.declined:
					try:
						msg = self._render_failure_message()
						ref_doc.flags.payment_failure_message = msg
						ref_doc.run_method("on_payment_failed", msg)
					except Exception:
						# Ensure no details are leaked to the client
						frappe.local.message_log = []
						psl.log_error("Setting failure message on ref doc failed")

			except PayloadIntegrityError:
				error = psl.log_error("Response validation failure")
				if not mute:
					return make_error_processed(error, _("There's been an issue with your payment."))
				return make_error_processed(error, _("Payment processing failed"))

			except PaymentControllerProcessingError as e:
				error = psl.log_error(title=f"Processing error ({e.psltype})", message=frappe.get_traceback())
				# "Unresolved", not "Error": by the time this runs the gateway has
				# answered — its response failed to process, or it reported a status
				# this controller does not map, which may well be a success we did
				# not recognise. "Error" is in RETRYABLE_STATES, so writing it here
				# left the session re-chargeable with its initiation payload intact
				# and the next proceed() started a second real charge.
				psl.set_processing_payload(response, "Unresolved")
				if not mute:
					return make_error_processed(error, _error_value(error, e.psltype))
				return make_error_processed(error, _("Payment processing failed"))

			except RefDocHookProcessingError as e:
				error = psl.log_error(
					title=f"Processing failure ({e.psltype} - refdoc hook)",
					message=frappe.get_traceback(),
				)
				# Record the reconciliation failure WITHOUT touching status: the
				# gateway's outcome (e.g. Paid) is the only record that the money
				# moved. Overwriting it with "Error - RefDoc" destroyed that record,
				# marked the session settled so it could not be re-driven, and made
				# it purgeable. Passing psl.status keeps the gateway truth intact.
				psl.set_processing_payload(response, psl.status)
				psl.db_set(
					{
						"reconciliation": "Failed",
						"reconciliation_error": error.name,
						# Keep the text on the log too: frappe clears Error Log after
						# 14 days while this row is retained until reconciliation
						# succeeds, so the Link alone would dangle exactly when it
						# matters most.
						"reconciliation_error_message": frappe.get_traceback(),
					},
					commit=True,
				)
				if not mute:
					return make_error_processed(error, _error_value(error, f"{e.psltype} (via ref doc hook)"))
				return make_error_processed(error, _("Payment processing failed"))

			except frappe.Redirect:
				# Control flow, not a failure — never swallow it.
				raise

			except Exception:
				# Gateways implement _validate_response, _render_failure_message and
				# the rest. Anything they raise that is not one of the three handled
				# types used to propagate out of process_response, leaving the session
				# at "Initiated" with no Error Log: a reachable state with no exit, and
				# on the webhook path a 500 that the gateway retries into forever.
				error = psl.log_error(title="Unhandled processing failure", message=frappe.get_traceback())
				# Preserve a gateway outcome that is already recorded, exactly as the
				# RefDocHookProcessingError handler above does: overwriting a
				# persisted Paid with "Error" destroys the record that money moved.
				#
				# is_terminal() is the display predicate, and the question here is
				# "has the gateway already said something?". The two coincide because
				# every state in TERMINAL_STATES is a gateway outcome — but that is a
				# coincidence of the current vocabulary, not a contract. If a
				# display-terminal state is ever added that is NOT a gateway outcome,
				# this line and its twin below need their own predicate.
				psl.set_processing_payload(response, psl.status if psl.is_terminal() else "Error")
				if not mute:
					return make_error_processed(error, _error_value(error, "charge"))
				return make_error_processed(error, _("Payment processing failed"))
			else:
				return processed

		except frappe.Redirect:
			raise

		except Exception:
			# Everything above the inner try — reloading the PSL, rebuilding TxData,
			# fetching the reference document — used to run with only a `finally`
			# here, so an ordinary failure (a reference document deleted between
			# initiate and callback; a stored tx_data key TxData no longer accepts)
			# escaped and stranded the session at "Initiated" with no Error Log. That
			# is the state the inner catch-all exists to prevent, reached by a
			# different door.
			#
			# make_error_processed is deliberately NOT used: it dereferences
			# self.state.tx_data, which is exactly what may not be set yet.
			error = psl.log_error(
				title="Unhandled failure before response processing",
				message=frappe.get_traceback(),
			)
			if not psl.is_terminal():
				psl.db_set("status", "Error", commit=True)
			return Processed(
				message=_("Payment processing failed"),
				action=dict(href="/", label=_("Go to Homepage")),
				status_changed_to=psl.status,
				indicator_color="red",
				payload={"error_ref": error_ref(error)},
			)

		finally:
			lock.__exit__(None, None, None)

	# Lifecycle hooks (contracts)
	#  - implement them for your controller
	# ---------------------------------------

	def validate_tx_data(self, tx_data: TxData) -> None:
		"""Invoked by the reference document for example in order to validate the transaction data.

		Should throw on error with an informative user facing message.
		"""
		raise NotImplementedError

	def is_user_flow_initiation_delegated(self, psl_name: PSLName) -> bool:
		"""If true, you should initiate the user flow from the Ref Doc.

		For example, by sending an email (with a payment url), letting the user make a phone call or initiating a factoring process.

		If false, the gateway initiates the user flow.
		"""
		return False

	# Concrete controller methods
	#  - implement them for your gateway
	# ---------------------------------------

	def _patch_tx_data(self, tx_data: TxData) -> TxData:
		"""Optional: Implement tx_data preprocessing if required by the gateway.
		For example in order to fix rounding or decimal accuracy.
		"""
		return tx_data

	def _pre_data_capture_hook(self) -> dict:
		"""Optional: Implement additional server side control flow prior to data capture.
		For example in order to fetch additional data from the gateway that must be already present
		during the data capture.

		This is NOT used in Buttons with the Third Party Widget implementation variant.
		"""
		return {}

	def _initiate_charge(self) -> Initiated:
		"""Invoked by proceed in order to initiate a charge flow.

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		"""
		raise NotImplementedError

	def _validate_response(self) -> None:
		"""Implement how the validation of the response signature

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		- self.state.response
		"""
		raise NotImplementedError

	def _process_response_for_charge(self) -> Processed | None:
		"""Implement how the controller should process charge responses

		Needs to be idempotent.

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		- self.state.response
		"""
		raise NotImplementedError

	def _render_failure_message(self) -> str:
		"""Extract a readable failure message out of the server response

		Implementations can read:
		- self.state.psl
		- self.state.tx_data
		- self.state.response
		"""
		raise NotImplementedError

	def _is_server_to_server(self) -> bool:
		"""If this is a server to server processing flow.

		In this case, no errors will be returned.

		Implementations can read:
		- self.state.response
		"""
		raise NotImplementedError


@frappe.whitelist()
def frontend_defaults(doctype: str):
	if not isinstance(doctype, str):
		frappe.throw(_("Invalid parameter"), frappe.ValidationError)

	# Only allow DocTypes that are registered as payment gateways
	if not frappe.db.exists("Payment Gateway", {"gateway_settings": doctype}):
		frappe.throw(_("Not a valid payment gateway"), frappe.ValidationError)

	c: PaymentController = get_controller(doctype)
	if issubclass(c, PaymentController):
		d: FrontendDefaults = c.frontend_defaults
		return d.__dict__
