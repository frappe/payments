# Copyright (c) 2026, Frappe and contributors
# For license information, please see LICENSE
"""A minimal, dependency-free PaymentController used as a reference
implementation and as the test vehicle for the framework. It performs no
network I/O: the charge outcome is taken from the response payload's
``status`` field, mapped through ``flowstates``.
"""

import frappe
from frappe import _

from payments.controllers import PaymentController
from payments.types import (
	FrontendDefaults,
	Initiated,
	Processed,
	RemoteServerInitiationPayload,
	SessionStates,
)


class PaymentDemoSettings(PaymentController):
	flowstates = SessionStates(
		success=["succeeded"],
		pre_authorized=["authorized"],
		processing=["pending"],
		declined=["failed"],
	)
	frontend_defaults = FrontendDefaults(
		gateway_css="",
		gateway_js="",
		gateway_wrapper="<div id='demo-gateway'></div>",
	)

	# -- contracts --

	def validate_tx_data(self, tx_data) -> None:
		if tx_data.amount is None or tx_data.amount <= 0:
			frappe.throw(_("Amount must be positive"))

	def _initiate_charge(self) -> Initiated:
		psl = self.state.psl
		return Initiated(
			correlation_id=f"demo-{psl.name}",
			payload=RemoteServerInitiationPayload({"demo": True, "psl": psl.name}),
		)

	def _validate_response(self) -> None:
		return None

	def _process_response_for_charge(self) -> Processed | None:
		payload = self.state.response.payload
		self.flags.status_changed_to = payload.get("status", "succeeded")
		return None

	def _render_failure_message(self) -> str:
		return self.state.response.payload.get("decline_reason", _("Demo payment failed"))

	def _is_server_to_server(self) -> bool:
		return bool(self.state.response.payload.get("s2s"))
