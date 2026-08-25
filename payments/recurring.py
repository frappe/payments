# Copyright (c) 2026, Frappe Technologies and contributors
# License: MIT. See LICENSE

"""Provider-neutral recurring-payment contract.

This module deliberately contains no provider, webhook, persistence, or business
subscription policy. Callers own their agreement records and providers implement
the controller methods dispatched here.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from frappe.utils import call_hook_method

from payments.utils.utils import get_payment_gateway_controller

__all__ = [
	"CONTRACT_VERSION",
	"EVENT_TYPES",
	"MANDATE_STATUSES",
	"PAYMENT_STATUSES",
	"SUBSCRIPTION_STATUSES",
	"FrozenDict",
	"FrozenList",
	"RecurringPaymentCapabilityError",
	"RecurringPaymentValidationError",
	"activate_subscription",
	"begin_first_payment",
	"cancel_subscription",
	"emit_recurring_event",
	"get_recurring_webhook_url",
	"normalize_provider_events",
	"reconcile_subscription",
	"retry_payment",
]

CONTRACT_VERSION = 1

PAYMENT_STATUSES = frozenset(
	{
		"open",
		"pending",
		"authorized",
		"paid",
		"failed",
		"canceled",
		"expired",
		"partially_refunded",
		"refunded",
		"partially_charged_back",
		"charged_back",
	}
)
SUBSCRIPTION_STATUSES = frozenset(
	{"pending", "pending_mandate", "active", "suspended", "canceled", "completed", "failed"}
)
MANDATE_STATUSES = frozenset({"pending", "valid", "invalid", "revoked", "expired"})
EVENT_TYPES = frozenset(
	{
		"first_payment.updated",
		"mandate.updated",
		"subscription.updated",
		"recurring_payment.updated",
	}
)

_MAX_REFERENCE_LENGTH = 255
_MAX_DESCRIPTION_LENGTH = 500
_MAX_URL_LENGTH = 2048
_MAX_METADATA_KEYS = 32
_MAX_METADATA_ITEMS = 32
_MAX_METADATA_DEPTH = 4
_MAX_METADATA_BYTES = 4096
_MAX_PROVIDER_EVENT_BYTES = 65536
_MAX_PROVIDER_EVENT_DEPTH = 12
_MAX_PROVIDER_EVENT_ITEMS = 256
_INTERVAL_RE = re.compile(r"P[1-9][0-9]*[DWMY]")
_AMOUNT_RE = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_CUSTOMER_LOCALE_RE = re.compile(r"[a-z]{2}_[A-Z]{2}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class RecurringPaymentValidationError(ValueError):
	"""Raised when a request or provider result violates contract version 1."""


class RecurringPaymentCapabilityError(NotImplementedError):
	"""Raised when a payment-gateway controller does not implement an operation."""


class FrozenDict(dict):
	"""A JSON-serializable dict whose normalized contents cannot be changed."""

	def _immutable(self, *args, **kwargs):
		raise TypeError("normalized recurring-payment values are immutable")

	__delitem__ = _immutable
	__ior__ = _immutable
	__setitem__ = _immutable
	clear = _immutable
	pop = _immutable
	popitem = _immutable
	setdefault = _immutable
	update = _immutable

	def __copy__(self):
		return self

	def __deepcopy__(self, memo):
		return self


class FrozenList(list):
	"""A JSON-serializable list whose normalized contents cannot be changed."""

	def _immutable(self, *args, **kwargs):
		raise TypeError("normalized recurring-payment values are immutable")

	__delitem__ = _immutable
	__iadd__ = _immutable
	__imul__ = _immutable
	__setitem__ = _immutable
	append = _immutable
	clear = _immutable
	extend = _immutable
	insert = _immutable
	pop = _immutable
	remove = _immutable
	reverse = _immutable
	sort = _immutable

	def __copy__(self):
		return self

	def __deepcopy__(self, memo):
		return self


# Exact version-1 request fields. Optional fields are listed separately; all
# other keys are rejected before a controller is invoked.
_FIRST_PAYMENT_REQUIRED = frozenset(
	{
		"merchant_reference",
		"customer_reference",
		"amount",
		"currency",
		"description",
		"redirect_url",
		"webhook_url",
		"idempotency_key",
	}
)
_FIRST_PAYMENT_OPTIONAL = frozenset(
	{
		"contract_version",
		"customer_email",
		"customer_locale",
		"customer_name",
		"metadata",
		"provider_customer_id",
	}
)
_ACTIVATE_SUBSCRIPTION_REQUIRED = frozenset(
	{
		"merchant_reference",
		"customer_reference",
		"provider_customer_id",
		"provider_mandate_id",
		"mandate_status",
		"amount",
		"currency",
		"interval",
		"start_date",
		"description",
		"webhook_url",
		"idempotency_key",
	}
)
_ACTIVATE_SUBSCRIPTION_OPTIONAL = frozenset({"contract_version", "metadata", "payment_count"})
_CANCEL_SUBSCRIPTION_REQUIRED = frozenset(
	{
		"merchant_reference",
		"customer_reference",
		"provider_customer_id",
		"provider_subscription_id",
		"idempotency_key",
	}
)
_CANCEL_SUBSCRIPTION_OPTIONAL = frozenset({"contract_version", "metadata"})
_RETRY_PAYMENT_REQUIRED = frozenset(
	{
		"merchant_reference",
		"customer_reference",
		"provider_payment_id",
		"idempotency_key",
	}
)
_RETRY_PAYMENT_OPTIONAL = frozenset({"contract_version", "metadata"})
_RECONCILE_SUBSCRIPTION_REQUIRED = frozenset(
	{"merchant_reference", "customer_reference", "provider_customer_id", "provider_subscription_id"}
)
_RECONCILE_SUBSCRIPTION_OPTIONAL = frozenset({"contract_version", "metadata"})

_PAYMENT_RESULT_FIELDS = frozenset(
	{
		"contract_version",
		"provider_payment_id",
		"status",
		"amount",
		"currency",
		"refunded_amount",
		"charged_back_amount",
		"merchant_reference",
		"customer_reference",
		"checkout_url",
		"provider_customer_id",
		"provider_mandate_id",
		"provider_subscription_id",
		"occurred_at",
		"metadata",
	}
)
_SUBSCRIPTION_RESULT_FIELDS = frozenset(
	{
		"contract_version",
		"provider_subscription_id",
		"status",
		"merchant_reference",
		"customer_reference",
		"provider_customer_id",
		"provider_mandate_id",
		"amount",
		"currency",
		"interval",
		"start_date",
		"description",
		"payment_count",
		"next_payment_at",
		"canceled_at",
		"occurred_at",
		"metadata",
	}
)
_MANDATE_RESULT_FIELDS = frozenset(
	{
		"contract_version",
		"provider_mandate_id",
		"status",
		"merchant_reference",
		"customer_reference",
		"provider_customer_id",
		"occurred_at",
		"metadata",
	}
)
_EVENT_FIELDS = frozenset(
	{
		"contract_version",
		"event_id",
		"event_type",
		"payment_gateway",
		"merchant_reference",
		"customer_reference",
		"provider_created_at",
		"payment",
		"mandate",
		"subscription",
		"metadata",
	}
)


def begin_first_payment(payment_gateway: str, request: Mapping[str, Any]) -> FrozenDict:
	"""Begin an on-session first payment used to establish a mandate."""
	gateway = _normalize_reference(payment_gateway, "payment_gateway", max_length=140)
	normalized = _normalize_first_payment_request(request)
	result = _dispatch(gateway, "begin_first_payment", normalized)
	return _normalize_payment_snapshot(result, expected=normalized)


def get_recurring_webhook_url(payment_gateway: str) -> str:
	"""Return a controller-owned HTTPS endpoint for recurring provider events."""
	gateway = _normalize_reference(payment_gateway, "payment_gateway", max_length=140)
	controller = get_payment_gateway_controller(gateway)
	method = getattr(controller, "get_recurring_webhook_url", None)
	if not callable(method):
		raise RecurringPaymentCapabilityError(
			f"Payment gateway {gateway!r} does not support recurring operation 'get_recurring_webhook_url'"
		)
	return _normalize_url(method(), "recurring webhook URL")


def activate_subscription(payment_gateway: str, request: Mapping[str, Any]) -> FrozenDict:
	"""Activate recurring collection after an authoritative valid mandate.

	The required ``mandate_status`` field must be exactly ``valid``. This core
	does not discover or infer mandate validity.
	"""
	gateway = _normalize_reference(payment_gateway, "payment_gateway", max_length=140)
	normalized = _normalize_activate_subscription_request(request)
	result = _dispatch(gateway, "activate_subscription", normalized)
	return _normalize_subscription_snapshot(result, expected=normalized)


def cancel_subscription(payment_gateway: str, request: Mapping[str, Any]) -> FrozenDict:
	"""Cancel a provider subscription idempotently."""
	gateway = _normalize_reference(payment_gateway, "payment_gateway", max_length=140)
	normalized = _normalize_reference_request(
		request,
		operation="cancel_subscription",
		required=_CANCEL_SUBSCRIPTION_REQUIRED,
		optional=_CANCEL_SUBSCRIPTION_OPTIONAL,
		provider_field="provider_subscription_id",
		idempotent=True,
	)
	result = _dispatch(gateway, "cancel_subscription", normalized)
	return _normalize_subscription_snapshot(result, expected=normalized)


def retry_payment(payment_gateway: str, request: Mapping[str, Any]) -> FrozenDict:
	"""Ask a capable provider to retry one known payment.

	Missing provider capability raises :class:`RecurringPaymentCapabilityError`.
	The dispatcher never creates a replacement payment as a fallback.
	"""
	gateway = _normalize_reference(payment_gateway, "payment_gateway", max_length=140)
	normalized = _normalize_reference_request(
		request,
		operation="retry_payment",
		required=_RETRY_PAYMENT_REQUIRED,
		optional=_RETRY_PAYMENT_OPTIONAL,
		provider_field="provider_payment_id",
		idempotent=True,
	)
	result = _dispatch(gateway, "retry_payment", normalized)
	return _normalize_payment_snapshot(result, expected=normalized)


def reconcile_subscription(payment_gateway: str, request: Mapping[str, Any]) -> FrozenDict:
	"""Fetch an authoritative subscription snapshot for consumer-led recovery."""
	gateway = _normalize_reference(payment_gateway, "payment_gateway", max_length=140)
	normalized = _normalize_reference_request(
		request,
		operation="reconcile_subscription",
		required=_RECONCILE_SUBSCRIPTION_REQUIRED,
		optional=_RECONCILE_SUBSCRIPTION_OPTIONAL,
		provider_field="provider_subscription_id",
		idempotent=False,
	)
	result = _dispatch(gateway, "reconcile_subscription", normalized)
	return _normalize_subscription_snapshot(result, expected=normalized)


def normalize_provider_events(payment_gateway: str, provider_event: Mapping[str, Any]) -> list[FrozenDict]:
	"""Delegate authoritative provider data and validate normalized events.

	``provider_event`` is transport-specific and intentionally has no field
	schema, but it must be bounded JSON data. Webhook implementations must first
	authenticate or fetch authoritative remote resources before calling this API.
	"""
	gateway = _normalize_reference(payment_gateway, "payment_gateway", max_length=140)
	if not isinstance(provider_event, Mapping):
		raise RecurringPaymentValidationError("provider_event must be a mapping")
	payload = _freeze_json(
		provider_event,
		"provider_event",
		max_depth=_MAX_PROVIDER_EVENT_DEPTH,
		max_items=_MAX_PROVIDER_EVENT_ITEMS,
		max_bytes=_MAX_PROVIDER_EVENT_BYTES,
	)
	result = _dispatch(gateway, "normalize_provider_events", payload)
	if not isinstance(result, list):
		raise RecurringPaymentValidationError("normalize_provider_events must return a list")
	return [_normalize_event(item, payment_gateway=gateway) for item in result]


def emit_recurring_event(event: Mapping[str, Any]) -> FrozenDict:
	"""Validate and synchronously deliver one event through the generic hook."""
	normalized = _normalize_event(event)
	call_hook_method("recurring_payment_event", event=normalized)
	return normalized


def _dispatch(payment_gateway: str, operation: str, request: FrozenDict):
	controller = get_payment_gateway_controller(payment_gateway)
	method = getattr(controller, operation, None)
	if not callable(method):
		raise RecurringPaymentCapabilityError(
			f"Payment gateway {payment_gateway!r} does not support recurring operation {operation!r}"
		)
	return method(request)


def _normalize_first_payment_request(request) -> FrozenDict:
	data = _validate_mapping_fields(
		request, "begin_first_payment", _FIRST_PAYMENT_REQUIRED, _FIRST_PAYMENT_OPTIONAL
	)
	if not data.get("provider_customer_id"):
		missing = [field for field in ("customer_name", "customer_email") if not data.get(field)]
		if missing:
			raise RecurringPaymentValidationError(
				"begin_first_payment requires customer_name and customer_email when "
				"provider_customer_id is absent"
			)

	normalized = {
		"contract_version": _normalize_version(data),
		"merchant_reference": _normalize_reference(data["merchant_reference"], "merchant_reference"),
		"customer_reference": _normalize_reference(data["customer_reference"], "customer_reference"),
		"amount": _normalize_amount(data["amount"]),
		"currency": _normalize_currency(data["currency"]),
		"description": _normalize_text(
			data["description"], "description", max_length=_MAX_DESCRIPTION_LENGTH
		),
		"redirect_url": _normalize_url(data["redirect_url"], "redirect_url", allow_local_http=True),
		"webhook_url": _normalize_url(data["webhook_url"], "webhook_url"),
		"idempotency_key": _normalize_reference(data["idempotency_key"], "idempotency_key"),
	}
	if data.get("provider_customer_id") is not None:
		normalized["provider_customer_id"] = _normalize_reference(
			data["provider_customer_id"], "provider_customer_id"
		)
	if data.get("customer_name") is not None:
		normalized["customer_name"] = _normalize_text(
			data["customer_name"], "customer_name", max_length=_MAX_REFERENCE_LENGTH
		)
	if data.get("customer_email") is not None:
		normalized["customer_email"] = _normalize_email(data["customer_email"])
	if data.get("customer_locale") is not None:
		normalized["customer_locale"] = _normalize_customer_locale(data["customer_locale"])
	normalized.update(_metadata_if_present(data))
	return _freeze(normalized)


def _normalize_activate_subscription_request(request) -> FrozenDict:
	data = _validate_mapping_fields(
		request,
		"activate_subscription",
		_ACTIVATE_SUBSCRIPTION_REQUIRED,
		_ACTIVATE_SUBSCRIPTION_OPTIONAL,
	)
	mandate_status = _normalize_choice(data["mandate_status"], "mandate_status", MANDATE_STATUSES)
	if mandate_status != "valid":
		raise RecurringPaymentValidationError(
			"activate_subscription requires an authoritative mandate_status of 'valid'"
		)
	return _freeze(
		{
			"contract_version": _normalize_version(data),
			"merchant_reference": _normalize_reference(data["merchant_reference"], "merchant_reference"),
			"customer_reference": _normalize_reference(data["customer_reference"], "customer_reference"),
			"provider_customer_id": _normalize_reference(
				data["provider_customer_id"], "provider_customer_id"
			),
			"provider_mandate_id": _normalize_reference(data["provider_mandate_id"], "provider_mandate_id"),
			"mandate_status": mandate_status,
			"amount": _normalize_amount(data["amount"]),
			"currency": _normalize_currency(data["currency"]),
			"interval": _normalize_interval(data["interval"]),
			"start_date": _normalize_date(data["start_date"], "start_date"),
			"description": _normalize_text(
				data["description"], "description", max_length=_MAX_DESCRIPTION_LENGTH
			),
			"webhook_url": _normalize_url(data["webhook_url"], "webhook_url"),
			"idempotency_key": _normalize_reference(data["idempotency_key"], "idempotency_key"),
			**_positive_int_if_present(data, "payment_count"),
			**_metadata_if_present(data),
		}
	)


def _normalize_reference_request(
	request,
	*,
	operation,
	required,
	optional,
	provider_field,
	idempotent,
) -> FrozenDict:
	data = _validate_mapping_fields(request, operation, required, optional)
	normalized = {
		"contract_version": _normalize_version(data),
		"merchant_reference": _normalize_reference(data["merchant_reference"], "merchant_reference"),
		"customer_reference": _normalize_reference(data["customer_reference"], "customer_reference"),
		provider_field: _normalize_reference(data[provider_field], provider_field),
	}
	if "provider_customer_id" in data:
		normalized["provider_customer_id"] = _normalize_reference(
			data["provider_customer_id"], "provider_customer_id"
		)
	if idempotent:
		normalized["idempotency_key"] = _normalize_reference(data["idempotency_key"], "idempotency_key")
	normalized.update(_metadata_if_present(data))
	return _freeze(normalized)


def _normalize_payment_snapshot(result, *, expected=None) -> FrozenDict:
	data = _validate_mapping_fields(
		result,
		"payment snapshot",
		frozenset(
			{
				"provider_payment_id",
				"status",
				"amount",
				"currency",
				"merchant_reference",
				"customer_reference",
			}
		),
		_PAYMENT_RESULT_FIELDS,
	)
	normalized = {
		"contract_version": _normalize_version(data),
		"provider_payment_id": _normalize_reference(data["provider_payment_id"], "provider_payment_id"),
		"status": _normalize_choice(data["status"], "payment status", PAYMENT_STATUSES),
		"amount": _normalize_amount(data["amount"]),
		"currency": _normalize_currency(data["currency"]),
		"merchant_reference": _normalize_reference(data["merchant_reference"], "merchant_reference"),
		"customer_reference": _normalize_reference(data["customer_reference"], "customer_reference"),
	}
	_optional_reference_fields(
		data,
		normalized,
		("provider_customer_id", "provider_mandate_id", "provider_subscription_id"),
	)
	for field in ("refunded_amount", "charged_back_amount"):
		if data.get(field) is not None:
			normalized[field] = _normalize_amount(data[field])
	_validate_payment_reversals(normalized)
	if data.get("checkout_url") is not None:
		normalized["checkout_url"] = _normalize_url(
			data["checkout_url"], "checkout_url", allow_local_http=True
		)
	if data.get("occurred_at") is not None:
		normalized["occurred_at"] = _normalize_timestamp(data["occurred_at"], "occurred_at")
	normalized.update(_metadata_if_present(data))
	_validate_correlation(
		normalized,
		expected,
		fields=(
			"merchant_reference",
			"customer_reference",
			"provider_payment_id",
			"provider_customer_id",
			"provider_mandate_id",
			"provider_subscription_id",
			"amount",
			"currency",
			"refunded_amount",
			"charged_back_amount",
		),
	)
	return _freeze(normalized)


def _normalize_subscription_snapshot(result, *, expected=None) -> FrozenDict:
	data = _validate_mapping_fields(
		result,
		"subscription snapshot",
		frozenset(
			{
				"provider_subscription_id",
				"status",
				"merchant_reference",
				"customer_reference",
				"provider_customer_id",
				"provider_mandate_id",
				"amount",
				"currency",
				"interval",
				"start_date",
				"description",
			}
		),
		_SUBSCRIPTION_RESULT_FIELDS,
	)
	normalized = {
		"contract_version": _normalize_version(data),
		"provider_subscription_id": _normalize_reference(
			data["provider_subscription_id"], "provider_subscription_id"
		),
		"status": _normalize_choice(data["status"], "subscription status", SUBSCRIPTION_STATUSES),
		"merchant_reference": _normalize_reference(data["merchant_reference"], "merchant_reference"),
		"customer_reference": _normalize_reference(data["customer_reference"], "customer_reference"),
		"provider_customer_id": _normalize_reference(data["provider_customer_id"], "provider_customer_id"),
		"provider_mandate_id": _normalize_reference(data["provider_mandate_id"], "provider_mandate_id"),
		"amount": _normalize_amount(data["amount"]),
		"currency": _normalize_currency(data["currency"]),
		"interval": _normalize_interval(data["interval"]),
		"start_date": _normalize_date(data["start_date"], "start_date"),
		"description": _normalize_text(
			data["description"], "description", max_length=_MAX_DESCRIPTION_LENGTH
		),
	}
	normalized.update(_positive_int_if_present(data, "payment_count"))
	if expected and "start_date" in expected:
		request_is_finite = "payment_count" in expected
		result_is_finite = "payment_count" in normalized
		if request_is_finite != result_is_finite:
			raise RecurringPaymentValidationError(
				"provider result changed finite payment_count schedule semantics"
			)
	for field in ("next_payment_at", "canceled_at", "occurred_at"):
		if data.get(field) is not None:
			normalized[field] = _normalize_timestamp(data[field], field)
	normalized.update(_metadata_if_present(data))
	_validate_correlation(
		normalized,
		expected,
		fields=(
			"merchant_reference",
			"customer_reference",
			"provider_subscription_id",
			"provider_customer_id",
			"provider_mandate_id",
			"amount",
			"currency",
			"interval",
			"start_date",
			"description",
			"payment_count",
		),
	)
	return _freeze(normalized)


def _normalize_mandate_snapshot(result) -> FrozenDict:
	data = _validate_mapping_fields(
		result,
		"mandate snapshot",
		frozenset(
			{
				"provider_mandate_id",
				"status",
				"merchant_reference",
				"customer_reference",
				"provider_customer_id",
			}
		),
		_MANDATE_RESULT_FIELDS,
	)
	normalized = {
		"contract_version": _normalize_version(data),
		"provider_mandate_id": _normalize_reference(data["provider_mandate_id"], "provider_mandate_id"),
		"status": _normalize_choice(data["status"], "mandate status", MANDATE_STATUSES),
		"merchant_reference": _normalize_reference(data["merchant_reference"], "merchant_reference"),
		"customer_reference": _normalize_reference(data["customer_reference"], "customer_reference"),
		"provider_customer_id": _normalize_reference(data["provider_customer_id"], "provider_customer_id"),
	}
	if data.get("occurred_at") is not None:
		normalized["occurred_at"] = _normalize_timestamp(data["occurred_at"], "occurred_at")
	normalized.update(_metadata_if_present(data))
	return _freeze(normalized)


def _normalize_event(event, payment_gateway=None) -> FrozenDict:
	data = _validate_mapping_fields(
		event,
		"recurring event",
		frozenset({"event_id", "event_type", "merchant_reference", "customer_reference"}),
		_EVENT_FIELDS,
	)
	provided_gateway = data.get("payment_gateway")
	if payment_gateway is None:
		gateway = _normalize_reference(provided_gateway, "payment_gateway", max_length=140)
	elif provided_gateway is not None and provided_gateway != payment_gateway:
		raise RecurringPaymentValidationError("provider event payment_gateway does not match dispatcher")
	else:
		gateway = payment_gateway

	normalized = {
		"contract_version": _normalize_version(data),
		"event_id": _normalize_reference(data["event_id"], "event_id"),
		"event_type": _normalize_choice(data["event_type"], "event_type", EVENT_TYPES),
		"payment_gateway": gateway,
		"merchant_reference": _normalize_reference(data["merchant_reference"], "merchant_reference"),
		"customer_reference": _normalize_reference(data["customer_reference"], "customer_reference"),
	}
	if data.get("provider_created_at") is not None:
		normalized["provider_created_at"] = _normalize_timestamp(
			data["provider_created_at"], "provider_created_at"
		)
	if data.get("payment") is not None:
		normalized["payment"] = _normalize_payment_snapshot(data["payment"])
	if data.get("mandate") is not None:
		normalized["mandate"] = _normalize_mandate_snapshot(data["mandate"])
	if data.get("subscription") is not None:
		normalized["subscription"] = _normalize_subscription_snapshot(data["subscription"])
	required_snapshot = {
		"first_payment.updated": "payment",
		"mandate.updated": "mandate",
		"subscription.updated": "subscription",
		"recurring_payment.updated": "payment",
	}[normalized["event_type"]]
	if required_snapshot not in normalized:
		raise RecurringPaymentValidationError(
			f"{normalized['event_type']} requires a {required_snapshot} snapshot"
		)
	snapshots = []
	for snapshot_name in ("payment", "mandate", "subscription"):
		if snapshot_name in normalized:
			snapshot = normalized[snapshot_name]
			snapshots.append(snapshot)
			_validate_correlation(
				snapshot,
				normalized,
				fields=("merchant_reference", "customer_reference"),
			)
	_validate_shared_snapshot_identifiers(snapshots)
	normalized.update(_metadata_if_present(data))
	return _freeze(normalized)


def _validate_mapping_fields(value, label, required, allowed):
	if not isinstance(value, Mapping):
		raise RecurringPaymentValidationError(f"{label} must be a mapping")
	keys = set(value)
	unknown = keys - set(required) - set(allowed)
	if unknown:
		raise RecurringPaymentValidationError(
			f"{label} contains unknown fields: {', '.join(sorted(map(str, unknown)))}"
		)
	missing = set(required) - keys
	if missing:
		raise RecurringPaymentValidationError(
			f"{label} is missing required fields: {', '.join(sorted(missing))}"
		)
	return value


def _normalize_version(data) -> int:
	version = data.get("contract_version", CONTRACT_VERSION)
	if type(version) is not int or version != CONTRACT_VERSION:
		raise RecurringPaymentValidationError(
			f"Unsupported recurring-payment contract_version {version!r}; expected {CONTRACT_VERSION}"
		)
	return CONTRACT_VERSION


def _normalize_amount(value) -> str:
	if not isinstance(value, str | Decimal) or isinstance(value, bool):
		raise RecurringPaymentValidationError(
			"amount must be a Decimal or decimal string; floats are forbidden"
		)
	if isinstance(value, str):
		# Bound work before regex/Decimal parsing and fixed-point formatting.
		if len(value) > 64 or not _AMOUNT_RE.fullmatch(value):
			raise RecurringPaymentValidationError(
				"amount must be a bounded canonical fixed-point decimal string"
			)
	try:
		amount = Decimal(value)
	except (InvalidOperation, ValueError):
		raise RecurringPaymentValidationError("amount must be a valid decimal")
	if not amount.is_finite() or amount <= 0:
		raise RecurringPaymentValidationError("amount must be finite and greater than zero")

	_, digits, exponent = amount.as_tuple()
	integer_digits = len(digits) + exponent if exponent >= 0 else max(len(digits) + exponent, 0)
	fraction_digits = max(-exponent, 0)
	if integer_digits > 18 or fraction_digits > 9 or len(digits) > 27:
		raise RecurringPaymentValidationError("amount exceeds 18 integer or 9 fractional digits")
	return format(amount, "f").lstrip("+")


def _normalize_currency(value) -> str:
	if not isinstance(value, str):
		raise RecurringPaymentValidationError("currency must be a three-letter string")
	currency = value.upper()
	if not _CURRENCY_RE.fullmatch(currency):
		raise RecurringPaymentValidationError("currency must be a three-letter ISO-style code")
	return currency


def _validate_payment_reversals(payment) -> None:
	amount = Decimal(payment["amount"])
	refunded = Decimal(payment["refunded_amount"]) if "refunded_amount" in payment else Decimal(0)
	charged_back = Decimal(payment["charged_back_amount"]) if "charged_back_amount" in payment else Decimal(0)
	if refunded > amount:
		raise RecurringPaymentValidationError("refunded_amount cannot exceed the original amount")
	if charged_back > amount:
		raise RecurringPaymentValidationError("charged_back_amount cannot exceed the original amount")
	if refunded + charged_back > amount:
		raise RecurringPaymentValidationError(
			"refunded_amount and charged_back_amount cannot exceed the original amount in total"
		)

	status = payment["status"]
	if charged_back:
		expected_status = "charged_back" if charged_back == amount else "partially_charged_back"
	elif refunded:
		expected_status = "refunded" if refunded == amount else "partially_refunded"
	else:
		expected_status = None
	if expected_status and status != expected_status:
		raise RecurringPaymentValidationError(
			f"payment status must be {expected_status!r} for its normalized reversal amounts"
		)
	if status in {"refunded", "partially_refunded"} and not refunded:
		raise RecurringPaymentValidationError(f"payment status {status!r} requires refunded_amount")
	if status in {"charged_back", "partially_charged_back"} and not charged_back:
		raise RecurringPaymentValidationError(f"payment status {status!r} requires charged_back_amount")


def _normalize_email(value) -> str:
	email = _normalize_text(value, "customer_email", max_length=320)
	if any(character.isspace() for character in email) or email.count("@") != 1:
		raise RecurringPaymentValidationError("customer_email must be a plain email address")
	local_part, domain = email.split("@")
	if not local_part or len(local_part) > 64 or not domain or len(domain) > 253:
		raise RecurringPaymentValidationError("customer_email must be a plain email address")
	return email


def _normalize_customer_locale(value) -> str:
	if not isinstance(value, str) or not _CUSTOMER_LOCALE_RE.fullmatch(value):
		raise RecurringPaymentValidationError(
			"customer_locale must use the portable ll_CC format, for example en_GB"
		)
	return value


def _normalize_reference(value, field, max_length=_MAX_REFERENCE_LENGTH) -> str:
	return _normalize_text(value, field, max_length=max_length)


def _normalize_text(value, field, *, max_length) -> str:
	if not isinstance(value, str) or not value or value != value.strip():
		raise RecurringPaymentValidationError(f"{field} must be a non-empty trimmed string")
	if len(value) > max_length:
		raise RecurringPaymentValidationError(f"{field} exceeds {max_length} characters")
	if _CONTROL_RE.search(value):
		raise RecurringPaymentValidationError(f"{field} contains control characters")
	return value


def _normalize_choice(value, field, choices) -> str:
	if not isinstance(value, str) or value not in choices:
		raise RecurringPaymentValidationError(f"{field} must be one of: {', '.join(sorted(choices))}")
	return value


def _normalize_interval(value) -> str:
	if not isinstance(value, str) or not _INTERVAL_RE.fullmatch(value):
		raise RecurringPaymentValidationError(
			"interval must be a single-unit ISO-8601 duration such as P1D, P2W, P1M, or P1Y"
		)
	return value


def _normalize_date(value, field) -> str:
	if not isinstance(value, str):
		raise RecurringPaymentValidationError(f"{field} must be an ISO date")
	try:
		parsed = date.fromisoformat(value)
	except ValueError:
		raise RecurringPaymentValidationError(f"{field} must be an ISO date")
	if parsed.isoformat() != value:
		raise RecurringPaymentValidationError(f"{field} must use YYYY-MM-DD")
	return value


def _normalize_timestamp(value, field) -> str:
	if not isinstance(value, str) or not value:
		raise RecurringPaymentValidationError(f"{field} must be an RFC-3339 timestamp")
	candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
	try:
		parsed = datetime.fromisoformat(candidate)
	except ValueError:
		raise RecurringPaymentValidationError(f"{field} must be an RFC-3339 timestamp")
	if parsed.tzinfo is None or parsed.utcoffset() is None:
		raise RecurringPaymentValidationError(f"{field} must include a timezone")
	return value


def _normalize_url(value, field, *, allow_local_http=False) -> str:
	value = _normalize_text(value, field, max_length=_MAX_URL_LENGTH)
	if "\\" in value:
		raise RecurringPaymentValidationError(f"{field} contains an unsafe backslash")
	try:
		parts = urlsplit(value)
		hostname = parts.hostname
		parts.port
		username = parts.username
		password = parts.password
	except ValueError:
		raise RecurringPaymentValidationError(f"{field} is not a valid absolute URL")
	if username is not None or password is not None:
		raise RecurringPaymentValidationError(f"{field} must not contain credentials")
	if not hostname or parts.fragment:
		raise RecurringPaymentValidationError(f"{field} must be an absolute URL without a fragment")
	local_hosts = {"localhost", "127.0.0.1", "::1"}
	if parts.scheme != "https" and not (
		allow_local_http and parts.scheme == "http" and hostname.lower() in local_hosts
	):
		raise RecurringPaymentValidationError(f"{field} must use HTTPS")
	return urlunsplit(parts)


def _metadata_if_present(data) -> dict:
	if "metadata" not in data:
		return {}
	if not isinstance(data["metadata"], Mapping):
		raise RecurringPaymentValidationError("metadata must be a mapping")
	return {
		"metadata": _freeze_json(
			data["metadata"],
			"metadata",
			max_depth=_MAX_METADATA_DEPTH,
			max_items=_MAX_METADATA_ITEMS,
			max_bytes=_MAX_METADATA_BYTES,
			max_keys=_MAX_METADATA_KEYS,
		)
	}


def _freeze_json(value, label, *, max_depth, max_items, max_bytes, max_keys=None):
	def visit(item, depth):
		if depth > max_depth:
			raise RecurringPaymentValidationError(f"{label} exceeds maximum nesting depth")
		if item is None or isinstance(item, str | bool | int):
			return item
		if isinstance(item, float):
			if not math.isfinite(item):
				raise RecurringPaymentValidationError(f"{label} contains a non-finite number")
			return item
		if isinstance(item, Mapping):
			if len(item) > (max_keys if depth == 0 and max_keys is not None else max_items):
				raise RecurringPaymentValidationError(f"{label} contains too many keys")
			copy = {}
			for key, child in item.items():
				if not isinstance(key, str) or not key or len(key) > 64 or _CONTROL_RE.search(key):
					raise RecurringPaymentValidationError(f"{label} keys must be short non-empty strings")
				copy[key] = visit(child, depth + 1)
			return _freeze(copy)
		if isinstance(item, list | tuple):
			if len(item) > max_items:
				raise RecurringPaymentValidationError(f"{label} contains too many list items")
			return _freeze_list([visit(child, depth + 1) for child in item])
		raise RecurringPaymentValidationError(f"{label} must contain only JSON-compatible values")

	frozen = visit(value, 0)
	try:
		encoded = json.dumps(frozen, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
	except (TypeError, ValueError):
		raise RecurringPaymentValidationError(f"{label} must be JSON serializable")
	if len(encoded) > max_bytes:
		raise RecurringPaymentValidationError(f"{label} exceeds {max_bytes} serialized bytes")
	return frozen


def _optional_reference_fields(data, normalized, fields):
	for field in fields:
		if data.get(field) is not None:
			normalized[field] = _normalize_reference(data[field], field)


def _positive_int_if_present(data, field) -> dict:
	if field not in data:
		return {}
	value = data[field]
	if type(value) is not int or value <= 0:
		raise RecurringPaymentValidationError(f"{field} must be a positive integer")
	return {field: value}


def _validate_shared_snapshot_identifiers(snapshots):
	for field in ("provider_customer_id", "provider_mandate_id", "provider_subscription_id"):
		values = {snapshot[field] for snapshot in snapshots if field in snapshot}
		if len(values) > 1:
			raise RecurringPaymentValidationError(
				f"event snapshots have mismatched shared provider identifier {field}"
			)


def _validate_correlation(actual, expected, *, fields):
	if not expected:
		return
	for field in fields:
		if field in expected and field in actual and expected[field] != actual[field]:
			raise RecurringPaymentValidationError(f"provider result changed correlated field {field}")


def _freeze(value: dict) -> FrozenDict:
	frozen = FrozenDict()
	for key, item in value.items():
		dict.__setitem__(frozen, key, item)
	return frozen


def _freeze_list(value: list) -> FrozenList:
	frozen = FrozenList()
	for item in value:
		list.append(frozen, item)
	return frozen
