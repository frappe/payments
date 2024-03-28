import json
import functools

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.integrations.utils import create_request_log

from types import MappingProxyType
from collections import namedtuple

InitiateReturnData = namedtuple("InitiateReturnData", ["correlation_id", "payload"])
ProcessReturnData = namedtuple("ProcessReturnData", ["message", "action", "payload"])
TxData = namedtuple(
	"TxData",
	[
		"amount",
		"currency",
		"reference_doctype",
		"reference_docname",
		"payer_contact",  # as: contact.as_dict()
		"payer_address",  # as: address.as_dict()
		# TODO: tx data for subscriptions, pre-authorized, require-mandate and other flows
	],
)


class ImmutableView(MappingProxyType):
	__getattr__ = MappingProxyType.get


def _error_value(error, flow):
	return _(
		"Our server had an issue processing your {0}. Please contact customer support mentioning: {1}"
	).format(flow, error)


def decorate_process_response(func):
	@functools.wraps(func)
	def wrapper(cls, integration_request_name: str, *, payload: dict):
		assert (
			cls.flags.success_states
		), "the controller must declare its `flags.success_states` as an iterable on the class"

		# Attention! mutable _integration_request is not part of the public state interface;
		cls._integration_request = frappe.get_doc("Integration Request", integration_request_name)
		cls.state.integration_request = ImmutableView(cls._integration_request.as_dict())
		cls.state.tx_data = ImmutableView(json.loads(cls._integration_request.data))  # QoL
		cls.state.response_payload = ImmutableView(payload)
		# guard against already processed or currently being processed payloads via another entrypoint
		try:
			cls._integration_request.lock(timeout=3)  # max processing allowance of alternative flow
		except frappe.DocumentLockedError:
			return cls.state.tx_data.get("saved_return_value")
		else:
			cls.validate_response_payload()

			# Attention! ref_doc not part of the public state interface;
			cls._ref_doc = frappe.get_doc(
				cls._integration_request.reference_doctype,
				cls._integration_request.reference_docname,
			)

			return_value = func(cls, payload)._asdict()
			cls._integration_request.update_status(
				{"saved_return_value": return_value}, cls._integration_request.status
			)
			return return_value

	return wrapper


class PaymentGatewayController(Document):
	"""This controller implemets the public API of payment gateway controllers."""

	def __init__(self, *args, **kwargs):
		super(Document, self).__init__(*args, **kwargs)
		self.state = frappe._dict()

	def on_refdoc_submission(self, tx_data: TxData) -> None:
		"""Invoked by the reference document for example in order to validate the transaction data.

		Should throw on error with an informative user facing message.

		Parameters:
		tx_data (dict): The transaction data for which to invoke this method

		Returns: None
		"""
		raise NotImplementedError

	def initiate_payment(
		self, tx_data: TxData, correlation_id: str | None = None, name: str | None = None
	) -> str:
		"""Standardized entrypoint to initiate a payment

		Inheriting methods can invoke super and then set e.g. request_id on self.state.integration_request to save
		and early-obtained correlation id from the payment gateway

		Parameters:
		tx_data 	(TxData): The transaction data for which to invoke this method
		correlation_id 	(str):	If this flow can be early initiated with the gateway without timeout penalty, then
		                        the correlation id may be already set here.
		name	(str): 	Some flows may need special names as tx ref (think: 6 digit pin code for SMS
		                confirmation or a 5 word menmonic)

		Returns: Integration Request Name
		"""

		# service_name is used here as short-cut to recover the controller from the tx reference (i.e. interation request name) from
		# the front end without the need for going over the reference document which may be hinreder by permissions or add latency
		self.state.integration_request = create_request_log(
			tx_data, service_name=f"{self.doctype}[{self.name}]", name=name
		)
		return self.state.integration_request.name

	def is_user_flow_initiation_delegated(self, integration_request_name: str) -> bool:
		"""Invoked by the reference document which initiates a payment integration request.

		Some old or exotic (think, for example: incasso/facturing/pos payment terminal) gateways may initate the user flow on their own terms.

		Parameters:
		integration_request_name (str): The unique integration request reference

		Returns:
		bool: Wether the reference document should initiate communication regarding the payment either automatically or
		      through a system user (e.g. make a phone call).
		"""
		return False

	def _should_have_mandate(self) -> bool:
		"""Invoked by `proceed` in order to deterine if the mandated flow branch ought to be elected

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data

		Returns: bool
		"""
		assert self.state.integration_request
		assert self.state.tx_data
		return False

	def _get_mandate(self) -> None | "Document":
		"""Invoked by `proceed` in order to deterine if, in the mandated flow branch, a mandate needs to be aquired first

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data

		Typically queries a custom mandate doctype that is specific to a particular payment gateway controller.

		Returns: None | Mandate()
		"""
		assert self.state.integration_request
		assert self.state.tx_data
		return None

	def _create_mandate(self) -> "Document":
		"""Invoked by `proceed` in order to create a mandate (in draft) for which a mandate aquisition is inminent

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data

		Returns: None | Mandate()
		"""
		assert self.state.integration_request
		assert self.state.tx_data
		return None

	def proceed(self, integration_request_name: str, updated_tx_data: dict) -> dict:
		"""Standardized entrypoint to submit a payment request for remote processing.

		It is invoked from a customer flow and thus catches errors into a friendly, non-sensitive message.

		Parameters:
		updated_tx_data (dict): Updates to the inital transaction data, can reflect customer choices and modify the flow

		Returns:
		dict:	{
		  type:		mandate_acquisition|mandated_charge|charge
		  mandate:	json of the mandate doc
		  txdata:	json of the current tx data
		  payload: 	Payload according to the needs of the specific gateway flow frontend implementation
		}
		"""
		integration_request = frappe.get_doc("Integration Request", integration_request_name)
		integration_request.update_status(updated_tx_data, "Queued")

		self.state.integration_request = ImmutableView(integration_request.as_dict())
		self.state.tx_data = ImmutableView(json.loads(integration_request.data))
		self.state.mandate = self._get_mandate()

		try:

			if self._should_have_mandate() and not self.mandate:
				self.state.mandate = self._create_mandate()
				correlation_id, payload = InitiateReturnData(**self._initiate_mandate_acquisition())
				integration_request.db_set("request_id", correlation_id)
				integration_request.update_status(
					{"saved_mandate": self.state.mandate.name}, integration_request.status
				)
				return {
					"type": "mandate_acquisition",
					"mandate": frappe.as_json(self.state.mandate),
					"txdata": frappe.as_json(self.state.tx_data | {}),
					"payload": payload,
				}
			elif self.state.mandate:
				correlation_id, payload = InitiateReturnData(**self._initiate_mandated_charge())
				integration_request.db_set("request_id", correlation_id)
				integration_request.update_status(
					{"saved_mandate": self.state.mandate.name}, integration_request.status
				)
				return {
					"type": "mandated_charge",
					"mandate": frappe.as_json(self.state.mandate),
					"txdata": frappe.as_json(self.state.tx_data | {}),
					"payload": payload,
				}
			else:
				correlation_id, payload = InitiateReturnData(**self._initiate_charge())
				integration_request.db_set("request_id", correlation_id, commit=True)
				return {
					"type": "charge",
					"txdata": frappe.as_json(self.state.tx_data | {}),
					"payload": payload,
				}

		except Exception:
			error = integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Payment Gateway Error"),
				_(
					"There has been an issue with the server's configuration for {0}. Please contact customer care mentioning: {1}"
				).format(self.name, error),
				http_status_code=401,
				indicator_color="yellow",
			)

	def _initiate_mandate_acquisition(self) -> dict:
		"""Invoked by proceed to initiate a mandate acquisiton flow.

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data

		Implementations can read/write:
		- self.state.mandate

		Returns (InitiateReturnData): (
		        correlation_id | None,
		        {} - gateway specific frontend data
		)
		"""
		assert (
			self.state.integration_request and self.state.tx_data and self.state.mandate
		), "Do not invoke _initiate_mandate_acquisition directly. It should be invoked by proceed"
		raise NotImplementedError

	def _initiate_mandated_charge(self) -> dict:
		"""Invoked by proceed or after having aquired a mandate in order to initiate a mandated charge flow.

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data

		Implementations can read/write:
		- self.state.mandate

		Returns (InitiateReturnData): (
		        correlation_id | None,
		        {} - gateway specific frontend data
		)
		"""
		assert (
			self.state.integration_request and self.state.tx_data and self.state.mandate
		), "Do not invoke _initiate_mandated_charge directly. It should be invoked by proceed"
		raise NotImplementedError

	def _initiate_charge(self) -> dict:
		"""Invoked by proceed in order to initiate a charge flow.

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data

		Returns (InitiateReturnData): (
		        correlation_id | None,
		        {} - gateway specific frontend data
		)
		"""
		assert (
			self.state.integration_request and self.state.tx_data
		), "Do not invoke _initiate_charge directly. It should be invoked by proceed"
		raise NotImplementedError

	def _validate_response_payload(self) -> None:
		raise NotImplementedError

	def validate_response_payload(self) -> None:
		"""Invoked by process_* functions.

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data
		- self.state.response_payload

		Return: None or Frappe Redirection Dict
		"""
		assert (
			self.state.integration_request and self.state.tx_data and self.state.response_payload
		), "Don't invoke controller.validate_payload directly. It is invoked by process_* functions."
		try:
			self._validate_response_payload()
		except Exception:
			error = self.state.integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Server Error"),
				_("There's been an issue with your payment."),
				http_status_code=500,
				indicator_color="red",
			)

	def _process_response_for_mandate_acquisition(self) -> None | dict:
		raise NotImplementedError

	@decorate_process_response
	def process_response_for_mandate_acquisition(self, payload: dict) -> ProcessReturnData:
		"""Invoked by the mandate acquisition flow coming from either the client (e.g. from ./checkout) or the gateway server (IPN).

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data
		- self.state.response_payload

		Implementations can read/write:
		- self.state.mandate

		Parameters:
		  integration_request_name (str): 	tx reference (added via and consumed by decorator)
		  payload (dict):			return payload from the flow (will be validated by decorator with validate_payload)

		Returns: (None or dict) Indicating the customer facing message and next action (redirect)
		"""
		self.state.mandate = self._get_mandate()

		return_value = None

		# dont' expose non-public mutable interfaces
		integration_request, ref_doc = self._integration_request, self._ref_doc
		del self.ref_doc, self._integration_request

		try:
			return_value = self._process_response_for_mandate_acquisition()
		except Exception:
			error = integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Server Error"),
				_error_value(error, "mandate acquisition"),
				http_status_code=500,
				indicator_color="red",
			)

		assert (
			self.flags.status_changed_to
		), "_process_response_for_mandate_acquisition must set self.flags.status_changed_to"

		try:
			if ref_doc.hasattr("on_payment_mandate_acquisition_processed"):
				return_value = (
					ref_doc.run_method(
						"on_payment_mandate_acquisition_processed", ImmutableView(self.flags), self.state
					)
					or return_value
				)
		except Exception:
			error = integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Server Error"),
				_error_value(error, "mandate acquisition (via ref doc hook)"),
				http_status_code=500,
				indicator_color="red",
			)

		if self.flags.status_changed_to in self.flags.success_states:
			integration_request.handle_success(self.state.response_payload)
			return_value = return_value or {
				"message": _("Payment mandate successfully acquired"),
				"action": {"redirect_to": "/"},
				"payload": None,
			}
			return ProcessReturnData(**return_value)
		else:
			assert (
				self.flags.pre_authorized_states
			), "the controller must declare its `flags.pre_authorized_states` as an iterable on the class, can be empty"
			if self.flags.status_changed_to in self.flags.pre_authorized_states:
				integration_request.handle_success(self.state.response_payload)
				integration_request.db_set("status", "Authorized", update_modified=False)
				return_value = return_value or {
					"message": _("Payment mandate successfully authorized"),
					"action": {"redirect_to": "/"},
					"payload": None,
				}
				return ProcessReturnData(**return_value)
			else:
				integration_request.handle_failure(self.state.response_payload)
				return_value = return_value or {
					"message": _("Payment mandate acquisition failed"),
					"action": {"redirect_to": "/"},
					"payload": None,
				}
				return ProcessReturnData(**return_value)

	def _process_response_for_mandated_charge(self) -> None | dict:
		raise NotImplementedError

	@decorate_process_response
	def process_response_for_mandated_charge(self, payload: dict) -> ProcessReturnData:
		"""Invoked by the mandated charge flow coming from either the client (e.g. from ./checkout) or the gateway server (IPN).

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data
		- self.state.response_payload

		Implementations can read/write:
		- self.state.mandate

		Parameters:
		  integration_request_name (str): 	tx reference (added via and consumed by decorator)
		  payload (dict):			return payload from the flow (will be validated by decorator with validate_payload)

		Returns: (None or dict) Indicating the customer facing message and next action (redirect)
		"""
		self.state.mandate = self._get_mandate()

		return_value = None

		# dont' expose non-public mutable interfaces
		integration_request, ref_doc = self._integration_request, self._ref_doc
		del self.ref_doc, self._integration_request

		try:
			return_value = self._process_response_for_mandated_charge()
		except Exception:
			error = integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Server Error"),
				_error_value(error, "mandated charge"),
				http_status_code=500,
				indicator_color="red",
			)

		assert (
			self.flags.status_changed_to
		), "_process_response_for_mandated_charge must set self.flags.status_changed_to"

		try:
			if ref_doc.hasattr("on_payment_mandated_charge_processed"):
				return_value = (
					ref_doc.run_method(
						"on_payment_mandated_charge_processed", ImmutableView(self.flags), self.state
					)
					or return_value
				)
		except Exception:
			error = integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Server Error"),
				_error_value(error, "mandated charge (via ref doc hook)"),
				http_status_code=500,
				indicator_color="red",
			)

		if self.flags.status_changed_to in self.flags.success_states:
			integration_request.handle_success(self.state.response_payload)
			return_value = return_value or {
				"message": _("Payment mandate charge succeeded"),
				"action": {"redirect_to": "/"},
				"payload": None,
			}
			return ProcessReturnData(**return_value)
		else:
			integration_request.handle_failure(self.state.response_payload)
			return_value = return_value or {
				"message": _("Payment mandate charge failed"),
				"action": {"redirect_to": "/"},
				"payload": None,
			}
			return ProcessReturnData(**return_value)

	def _process_response_for_charge(self) -> None | dict:
		raise NotImplementedError

	@decorate_process_response
	def process_response_for_charge(self, payload: dict) -> ProcessReturnData:
		"""Invoked by the charge flow coming from either the client (e.g. from ./checkout) or the gateway server (IPN).

		Implementations can read:
		- self.state.integration_request
		- self.state.tx_data
		- self.state.response_paymload

		Parameters:
		  integration_request_name (str): 	tx reference (added via and consumed by decorator)
		  payload (dict):			return payload from the flow (will be validated by decorator with validate_payload)

		Returns: (None or dict) Indicating the customer facing message and next action (redirect)
		"""

		return_value = None

		# dont' expose non-public mutable interfaces
		integration_request, ref_doc = self._integration_request, self._ref_doc
		del self.ref_doc, self._integration_request

		try:
			return_value = self._process_response_for_charge()
		except Exception:
			error = integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Server Error"),
				_error_value(error, "charge"),
				http_status_code=500,
				indicator_color="red",
			)

		assert (
			self.flags.status_changed_to
		), "_process_response_for_charge must set self.flags.status_changed_to"

		try:
			if ref_doc.hasattr("on_payment_charge_processed"):
				return_value = (
					ref_doc.run_method("on_payment_charge_processed", ImmutableView(self.flags), self.state)
					or return_value
				)
		except Exception:
			error = integration_request.log_error(frappe.get_traceback())
			frappe.redirect_to_message(
				_("Server Error"),
				_error_value(error, "charge (via ref doc hook)"),
				http_status_code=500,
				indicator_color="red",
			)

		if self.flags.status_changed_to in self.flags.success_states:
			integration_request.handle_success(self.state.response_payload)
			return_value = return_value or {
				"message": _("Payment charge succeeded"),
				"action": {"redirect_to": "/"},
				"payload": None,
			}
			return ProcessReturnData(**return_value)
		else:
			integration_request.handle_failure(self.state.response_payload)
			return_value = return_value or {
				"message": _("Payment charge failed"),
				"action": {"redirect_to": "/"},
				"payload": None,
			}
			return ProcessReturnData(**return_value)
