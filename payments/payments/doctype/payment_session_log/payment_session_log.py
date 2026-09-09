# Copyright (c) 2021, Frappe and contributors
# For license information, please see LICENSE

import dataclasses
import json
from typing import TYPE_CHECKING, ClassVar, TypedDict

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder import Interval
from frappe.query_builder.functions import Now

from payments.types import GatewayProcessingResponse, GatewayRef, RemoteServerInitiationPayload, TxData

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
		decline_reason: DF.Data | None
		flow_type: DF.Data | None
		gateway: DF.Data | None
		initiation_response_payload: DF.Code | None
		mandate: DF.Data | None
		processing_response_payload: DF.Code | None
		status: DF.Data | None
		title: DF.Data | None
		tx_data: DF.Code | None
	# end: auto-generated types

	# Centralized terminal state definitions - single source of truth
	# Used by /pay endpoint and other consumers
	TERMINAL_STATES: ClassVar[dict[str, str]] = {
		"Paid": "green",
		"Authorized": "green",
		"Processing": "yellow",
		"Declined": "red",
		"Cancelled": "red",
		"Error": "red",
		"Error - RefDoc": "red",
	}

	def is_terminal(self) -> bool:
		"""Check if PSL is in a terminal state (no further action possible)."""
		return self.status in self.TERMINAL_STATES

	def get_indicator_color(self) -> str:
		"""Get the indicator color for the current status."""
		return self.TERMINAL_STATES.get(self.status, "gray")

	def update_tx_data(self, tx_data: TxData, status: str) -> None:
		data = json.loads(self.tx_data)
		data.update(tx_data)
		self.db_set(
			{
				"tx_data": frappe.as_json(data),
				"status": status,
			},
			commit=True,
		)

	def update_gateway_specific_state(self, data: dict, status: str) -> None:
		"""Store gateway-specific state during data capture phase."""
		self.db_set(
			{
				"initiation_response_payload": frappe.as_json(data),
				"status": status,
			},
			commit=True,
		)

	def set_initiation_payload(self, initiation_payload: RemoteServerInitiationPayload, status: str) -> None:
		self.db_set(
			{
				"initiation_response_payload": frappe.as_json(initiation_payload),
				"status": status,
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

	def get_controller(self) -> "PaymentController":
		"""For perfomance reasons, this is not implemented as a dynamic link but a json value
		so that it is only fetched when absolutely necessary.
		"""
		if not self.gateway:
			self.log_error("No gateway selected yet")
			frappe.throw(_("No gateway selected for this payment session"))
		ref = GatewayRef.from_json(self.gateway)
		return frappe.get_cached_doc(ref.gateway_settings, ref.gateway_controller)

	def get_button(self) -> "PaymentButton":
		if not self.button:
			self.log_error("No button selected yet")
			frappe.throw(_("No button selected for this payment session"))
		return frappe.get_cached_doc("Payment Button", self.button)

	@staticmethod
	def clear_old_logs(days=90):
		table = frappe.qb.DocType("Payment Session Log")
		frappe.db.delete(
			table, filters=(table.modified < (Now() - Interval(days=days))) & (table.status == "Paid")
		)


@frappe.whitelist(allow_guest=True)
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
		e = frappe.log_error("Payment Session Log not found", reference_doctype="Payment Session Log")
		# Ensure no more details are leaked than the error log reference
		frappe.local.message_log = [_("Server Failure!<br>{}").format(e)]
		return

	# Validate PSL is in a state where button selection is allowed
	if psl.is_terminal():
		frappe.log_error(
			f"Attempted button selection on terminal PSL: {pslName} (status: {psl.status})",
			reference_doctype="Payment Session Log",
		)
		frappe.local.message_log = [_("This payment session is no longer active.")]
		return

	try:
		btn: PaymentButton = frappe.get_cached_doc("Payment Button", buttonName)
	except Exception:
		e = frappe.log_error("Payment Button not found", reference_doctype="Payment Button")
		# Ensure no more details are leaked than the error log reference
		frappe.local.message_log = [_("Server Failure!<br>{}").format(e)]
		return

	# Validate button is enabled
	if not btn.enabled:
		frappe.log_error(
			f"Attempted to select disabled button: {buttonName}",
			reference_doctype="Payment Button",
		)
		frappe.local.message_log = [_("This payment method is not available.")]
		return

	# Validate button matches PSL gateway filter (if set)
	if psl.gateway:
		try:
			gateway_filter = json.loads(psl.gateway)
			# Check if selected button matches the required gateway settings/controller
			if (
				gateway_filter.get("gateway_settings")
				and gateway_filter["gateway_settings"] != btn.gateway_settings
			):
				frappe.log_error(
					f"Button gateway mismatch: expected {gateway_filter.get('gateway_settings')}, got {btn.gateway_settings}",
					reference_doctype="Payment Session Log",
				)
				frappe.local.message_log = [_("This payment method is not available for this transaction.")]
				return
			if (
				gateway_filter.get("gateway_controller")
				and gateway_filter["gateway_controller"] != btn.gateway_controller
			):
				frappe.log_error(
					f"Button controller mismatch: expected {gateway_filter.get('gateway_controller')}, got {btn.gateway_controller}",
					reference_doctype="Payment Session Log",
				)
				frappe.local.message_log = [_("This payment method is not available for this transaction.")]
				return
		except (json.JSONDecodeError, TypeError):
			pass  # No valid gateway filter, allow any button

	psl.db_set(
		{
			"button": buttonName,
			"gateway": GatewayRef(btn.gateway_settings, btn.gateway_controller).to_json(),
		}
	)
	# once state set: reload the page to activate widget
	return {"reload": True}


def create_log(
	tx_data: TxData,
	controller: "PaymentController" = None,
	status: str = "Created",
) -> PaymentSessionLog:
	log = frappe.new_doc("Payment Session Log")
	# TxData is a dataclass — convert to dict for JSON serialization
	tx_data_dict = dataclasses.asdict(tx_data) if dataclasses.is_dataclass(tx_data) else tx_data
	log.tx_data = frappe.as_json(tx_data_dict)
	log.status = status
	if controller:
		log.gateway = GatewayRef(controller.doctype, controller.name).to_json()

	log.insert(ignore_permissions=True)
	return log
