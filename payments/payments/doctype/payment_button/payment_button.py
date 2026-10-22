# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# License: MIT. See LICENSE

import json

import frappe
from frappe import _
from frappe.model.document import Document

from payments.payments.doctype.payment_session_log.payment_session_log import PSLState
from payments.types import RemoteServerInitiationPayload, TxData

Css = str
Js = str
Wrapper = str


class PaymentButton(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		data_capture: DF.Code | None
		enabled: DF.Check
		extra_payload: DF.Code | None
		gateway_controller: DF.DynamicLink
		gateway_css: DF.Code | None
		gateway_js: DF.Code | None
		gateway_settings: DF.Link
		gateway_wrapper: DF.Code | None
		icon: DF.AttachImage | None
		implementation_variant: DF.Literal["Third Party Widget", "Data Capture"]
		label: DF.Data
	# end: auto-generated types

	# Frontend Assets (widget)
	#  - implement them for your controller
	#  - need to be fully rendered with
	# ---------------------------------------
	def _frontend_safe_doc(self) -> "frappe._dict":
		"""Frontend-safe projection of the gateway settings doc for templates.

		Security (H1): the raw gateway settings document holds API secrets
		(secret_key, webhook secrets, tokens). Since these templates are rendered
		into the guest /pay page, passing the raw doc would let any template leak
		credentials to every visitor. We expose ONLY the explicit, non-secret
		fields the controller whitelists via get_frontend_safe_context(); existing
		templates referencing `doc.<field>` keep working for whitelisted fields,
		while secrets are simply absent.
		"""
		controller = frappe.get_cached_doc(self.gateway_settings, self.gateway_controller)
		return frappe._dict(controller.get_frontend_safe_context())

	def get_widget_assets(self, payload: RemoteServerInitiationPayload) -> (Css, Js, Wrapper):
		"""Get the fully rendered frontend assets for this button.

		The gateway_css/js/wrapper fields are Jinja templates authored by a System
		Manager (the only role with write permission on Payment Button; Guest has
		none), so rendering them is intentional rather than user-controlled input.
		The data reaching them is the get_frontend_safe_context() projection.
		"""
		context = {
			"doc": self._frontend_safe_doc(),
			"payload": payload,
		}
		css = frappe.render_template(self.gateway_css, context)  # nosemgrep: frappe-ssti
		js = frappe.render_template(self.gateway_js, context)  # nosemgrep: frappe-ssti
		wrapper = frappe.render_template(self.gateway_wrapper, context)  # nosemgrep: frappe-ssti
		return css, js, wrapper

	def get_data_capture_assets(self, state: PSLState) -> Wrapper:
		"""Get the fully rendered data capture form.

		The rendering context is updated with `state`.
		"""
		context = {
			"doc": self._frontend_safe_doc(),
			"extra": frappe._dict(json.loads(self.extra_payload)),
		}
		context.update(state)
		# data_capture is a System-Manager-authored template, as above.
		return frappe.render_template(self.data_capture, context)  # nosemgrep: frappe-ssti

	@property
	def requires_data_capture(self):
		return self.implementation_variant == "Data Capture"

	def validate(self):
		if self.extra_payload:
			try:
				json.loads(self.extra_payload)
			except Exception:
				frappe.throw(_("Extra Payload must be valid JSON."))
