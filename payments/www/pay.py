import json
from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils.file_manager import get_file_path

from payments.controllers import PaymentController
from payments.types import Proceeded, RemoteServerInitiationPayload, TxData
from payments.utils import PAYMENT_SESSION_REF_KEY

if TYPE_CHECKING:
	from payments.payments.doctype.payment_button.payment_button import PaymentButton
	from payments.payments.doctype.payment_session_log.payment_session_log import PaymentSessionLog

no_cache = 1


def get_psl() -> "PaymentSessionLog":
	try:
		name = frappe.form_dict[PAYMENT_SESSION_REF_KEY]
		psl: PaymentSessionLog = frappe.get_doc("Payment Session Log", name)
		return psl
	except (KeyError, frappe.exceptions.DoesNotExistError):
		frappe.redirect_to_message(
			_("Invalid Payment Link"),
			_("This payment link is invalid!"),
			http_status_code=400,
			indicator_color="red",
		)
		raise frappe.Redirect


default_icon = """
<svg style="shape-rendering:geometricPrecision; text-rendering:geometricPrecision; image-rendering:optimizeQuality; fill-rule:evenodd; clip-rule:evenodd" version="1.1" viewBox="0 0 270.92 270.92">
<g id="Layer_x0020_1"><path class="fil0" d="M135.48 160.83c-4.8,0 -8.73,-3.91 -8.73,-8.7 0,-4.4 -3.53,-7.95 -7.93,-7.95 -4.39,0 -7.93,3.55 -7.93,7.95 0,10.75 6.99,19.83 16.65,23.15l0 4.49c0,4.38 3.55,7.95 7.94,7.95 4.38,0 7.93,-3.57 7.93,-7.95l0 -4.49c9.66,-3.32 16.65,-12.4 16.65,-23.15 0,-13.58 -11.03,-24.61 -24.58,-24.61 -4.8,0 -8.73,-3.91 -8.73,-8.71 0,-4.81 3.93,-8.72 8.73,-8.72 4.79,0 8.72,3.91 8.72,8.72 0,4.38 3.55,7.94 7.94,7.94 4.38,0 7.92,-3.56 7.92,-7.94 0,-10.77 -6.99,-19.83 -16.65,-23.16l0 -4.51c0,-4.38 -3.55,-7.94 -7.93,-7.94 -4.39,0 -7.94,3.56 -7.94,7.94l0 4.51c-9.66,3.33 -16.65,12.39 -16.65,23.16 0,13.56 11.02,24.58 24.59,24.58 4.79,0 8.72,3.91 8.72,8.74 0,4.79 -3.93,8.7 -8.72,8.7zm-69.24 46l-14.21 0c-10.9,-0.24 -19.72,-9.16 -19.72,-20.13l0 -13.76c17.12,3.25 30.66,16.79 33.93,33.89zm172.4 -33.89l0 13.76c0,10.97 -8.81,19.89 -19.7,20.13l-14.26 -0.01c3.27,-17.1 16.84,-30.65 33.96,-33.88zm-33.96 -108.91l13.79 0c11.1,0 20.14,9.04 20.16,20.14l-0.01 13.75c-17.11,-3.26 -30.67,-16.79 -33.94,-33.89zm17.56 -15.86l-170.55 0c-4.38,0 -7.94,3.56 -7.94,7.93 0,4.37 3.56,7.93 7.94,7.93l136.97 0c3.57,25.85 24.1,46.38 49.98,49.91l0 42.99c-25.9,3.54 -46.44,24.08 -49.98,49.95l-106.4 0c-3.54,-25.87 -24.06,-46.39 -49.95,-49.95l0 -73.49c0,-4.39 -3.56,-7.94 -7.94,-7.94 -4.39,0 -7.94,3.55 -7.94,7.94l0 82.36c0,0.13 -0.02,0.25 -0.02,0.4l0 24.24c0,17.79 14.47,32.27 32.28,32.27l3.34 0c0.15,0 0.3,0.04 0.45,0.04l165.99 0c0.15,0 0.31,-0.04 0.47,-0.04l3.3 0c17.81,0 32.27,-14.48 32.27,-32.27l0 -110.02c0,-17.77 -14.46,-32.25 -32.27,-32.25z"/></g>
</svg>
"""


def load_icon(icon_file):
	return frappe.read_file(get_file_path(icon_file)) if icon_file else default_icon


def get_context(context):
	# always

	# Debug mode only for authenticated users in developer mode
	context.debug = (
		frappe.form_dict.get("debug") == "1"
		and frappe.conf.get("developer_mode")
		and frappe.session.user != "Guest"
	)

	psl: PaymentSessionLog = get_psl()
	state = psl.load_state()
	context.tx_data: TxData = state.tx_data
	context.logo = frappe.get_website_settings("app_logo") or frappe.get_hooks("app_logo_url")[-1]

	# Not reached a terminal state, yet
	# A terminal error state would require operator intervention, first
	if not psl.is_terminal():
		# First Pass: chose payment button
		# gateway was preselected; e.g. on the backend
		filters = {"enabled": True}
		if psl.gateway:
			filters.update(json.loads(psl.gateway))

		buttons = frappe.get_list(
			"Payment Button",
			fields=["name", "icon", "label"],
			filters=filters,
		)

		# Use already-fetched buttons instead of re-querying
		context.payment_buttons = [
			(load_icon(entry.get("icon")), entry.get("name"), entry.get("label")) for entry in buttons
		]
		context.render_buttons = True

		if not psl.button:
			context.render_widget = False
			context.render_capture = False

		# Second Pass (Data Capture): capture additonal data if the button requires it
		elif psl.get_button().requires_data_capture:
			context.render_widget = False
			context.render_capture = True

			proceeded: Proceeded = PaymentController.pre_data_capture_hook(psl.name)
			# Display
			button: PaymentButton = psl.get_button()
			context.data_capture = button.get_data_capture_assets(state)
			context.button_name = psl.button

		# Second Pass (Third Party Widget): let the third party widget manage data capture and flow
		else:
			context.render_widget = True
			context.render_capture = False

			proceeded: Proceeded = PaymentController.proceed(psl.name)

			# Display
			payload: RemoteServerInitiationPayload = proceeded.payload
			button: PaymentButton = psl.get_button()
			css, js, wrapper = button.get_widget_assets(payload)
			context.gateway_css = css
			context.gateway_js = js
			context.gateway_wrapper = wrapper

	# Response processed already: show the result
	else:
		context.render_widget = False
		context.render_buttons = False
		context.render_capture = False
		context.status = psl.status
		context.indicator_color = psl.get_indicator_color()
