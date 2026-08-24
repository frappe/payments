# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE

import json
import unittest
from decimal import Decimal
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from payments.recurring import (
	CONTRACT_VERSION,
	FrozenDict,
	RecurringPaymentCapabilityError,
	RecurringPaymentValidationError,
	activate_subscription,
	begin_first_payment,
	cancel_subscription,
	emit_recurring_event,
	normalize_provider_events,
	reconcile_subscription,
	retry_payment,
)


def first_payment_request(**overrides):
	request = {
		"merchant_reference": "order/customer:42",
		"customer_reference": "customer/opaque:α",
		"amount": "12.50",
		"currency": "eur",
		"description": "First payment",
		"redirect_url": "https://shop.example.test/complete?order=42",
		"webhook_url": "https://shop.example.test/api/payment-webhook",
		"idempotency_key": "first/order:42",
		"metadata": {"tenant": "site-1", "flags": [1, True]},
	}
	request.update(overrides)
	return request


def payment_snapshot(**overrides):
	result = {
		"provider_payment_id": "pay/provider:1",
		"status": "open",
		"amount": "12.50",
		"currency": "EUR",
		"merchant_reference": "order/customer:42",
		"customer_reference": "customer/opaque:α",
		"checkout_url": "https://provider.example.test/pay/1",
		"provider_customer_id": "customer-provider/1",
		"occurred_at": "2026-06-01T12:30:00Z",
	}
	result.update(overrides)
	return result


def subscription_request(**overrides):
	request = {
		"merchant_reference": "agreement/42",
		"customer_reference": "customer/opaque:α",
		"provider_customer_id": "customer-provider/1",
		"provider_mandate_id": "mandate-provider/1",
		"mandate_status": "valid",
		"amount": Decimal("19.00"),
		"currency": "eur",
		"interval": "P1M",
		"start_date": "2026-07-01",
		"description": "Monthly access",
		"webhook_url": "https://shop.example.test/api/subscription-webhook",
		"idempotency_key": "activate/agreement:42",
	}
	request.update(overrides)
	return request


def subscription_snapshot(**overrides):
	result = {
		"provider_subscription_id": "subscription-provider/1",
		"status": "active",
		"merchant_reference": "agreement/42",
		"customer_reference": "customer/opaque:α",
		"provider_customer_id": "customer-provider/1",
		"provider_mandate_id": "mandate-provider/1",
		"amount": "19.00",
		"currency": "EUR",
		"interval": "P1M",
		"start_date": "2026-07-01",
		"description": "Monthly access",
		"next_payment_at": "2026-08-01T00:00:00+00:00",
	}
	result.update(overrides)
	return result


def cancellation_request(**overrides):
	request = {
		"merchant_reference": "agreement/42",
		"customer_reference": "customer/opaque:α",
		"provider_customer_id": "customer-provider/1",
		"provider_subscription_id": "subscription-provider/1",
		"idempotency_key": "cancel/agreement:42",
	}
	request.update(overrides)
	return request


def recurring_event(**overrides):
	event = {
		"event_id": "event/provider:1/paid",
		"event_type": "first_payment.updated",
		"payment_gateway": "Fake Gateway",
		"merchant_reference": "order/customer:42",
		"customer_reference": "customer/opaque:α",
		"provider_created_at": "2026-06-01T12:30:00Z",
		"payment": payment_snapshot(status="paid"),
	}
	event.update(overrides)
	return event


class RecurringContractTestCase(unittest.TestCase):
	def controller_for(self, **methods):
		controller = type("FakeController", (), {})()
		for name, implementation in methods.items():
			setattr(controller, name, MagicMock(side_effect=implementation))
		return controller

	def dispatch_with(self, controller, callable, *args):
		with patch("payments.recurring.get_payment_gateway_controller", return_value=controller) as resolver:
			result = callable(*args)
		resolver.assert_called_once_with(args[0])
		return result


class TestFirstPayment(RecurringContractTestCase):
	def test_dispatches_only_through_the_gateway_controller_with_a_normalized_copy(self):
		original = first_payment_request()

		def create(request):
			self.assertIsInstance(request, FrozenDict)
			self.assertEqual(request["contract_version"], CONTRACT_VERSION)
			self.assertEqual(request["amount"], "12.50")
			self.assertEqual(request["currency"], "EUR")
			self.assertEqual(request["customer_reference"], "customer/opaque:α")
			self.assertIsNot(request["metadata"], original["metadata"])
			with self.assertRaises(TypeError):
				request["currency"] = "USD"
			with self.assertRaises(TypeError):
				request["metadata"]["tenant"] = "changed"
			return payment_snapshot()

		controller = self.controller_for(begin_first_payment=create)
		result = self.dispatch_with(controller, begin_first_payment, "Fake Gateway", original)

		self.assertEqual(result["contract_version"], CONTRACT_VERSION)
		self.assertEqual(original["currency"], "eur")
		self.assertEqual(original["metadata"]["tenant"], "site-1")
		self.assertEqual(json.loads(json.dumps(result))["provider_payment_id"], "pay/provider:1")
		with self.assertRaises(TypeError):
			result["status"] = "paid"

	def test_accepts_decimal_and_rejects_float_zero_negative_and_non_finite_amounts(self):
		controller = self.controller_for(
			begin_first_payment=lambda request: payment_snapshot(amount=request["amount"])
		)
		result = self.dispatch_with(
			controller, begin_first_payment, "Fake Gateway", first_payment_request(amount=Decimal("1.230"))
		)
		self.assertEqual(result["amount"], "1.230")

		for amount in (
			1.25,
			0,
			"0",
			"-1",
			"NaN",
			"Infinity",
			"1e2",
			"01.00",
			"1.1234567890",
			"1" * 100_000,
			Decimal("1E+1000000"),
			Decimal("1E-1000000"),
		):
			with self.subTest(amount=amount):
				with patch("payments.recurring.get_payment_gateway_controller") as resolver:
					with self.assertRaises(RecurringPaymentValidationError):
						begin_first_payment("Fake Gateway", first_payment_request(amount=amount))
				resolver.assert_not_called()

	def test_normalizes_three_letter_currency_and_rejects_other_values(self):
		controller = self.controller_for(
			begin_first_payment=lambda request: payment_snapshot(currency=request["currency"])
		)
		self.dispatch_with(
			controller, begin_first_payment, "Fake Gateway", first_payment_request(currency="usd")
		)
		request = controller.begin_first_payment.call_args.args[0]
		self.assertEqual(request["currency"], "USD")

		for currency in ("EU", "EURO", "12A", 123):
			with self.subTest(currency=currency):
				with self.assertRaises(RecurringPaymentValidationError):
					begin_first_payment("Fake Gateway", first_payment_request(currency=currency))

	def test_rejects_unknown_missing_and_unsupported_version_fields_before_dispatch(self):
		invalid = (
			first_payment_request(extra="not in v1"),
			{key: value for key, value in first_payment_request().items() if key != "customer_reference"},
			first_payment_request(contract_version=2),
			first_payment_request(contract_version=1.0),
		)
		for request in invalid:
			with self.subTest(request=request):
				with patch("payments.recurring.get_payment_gateway_controller") as resolver:
					with self.assertRaises(RecurringPaymentValidationError):
						begin_first_payment("Fake Gateway", request)
				resolver.assert_not_called()

	def test_safe_urls_require_https_except_for_local_redirect_development(self):
		controller = self.controller_for(begin_first_payment=lambda request: payment_snapshot())
		self.dispatch_with(
			controller,
			begin_first_payment,
			"Fake Gateway",
			first_payment_request(redirect_url="http://127.0.0.1:8000/complete"),
		)

		for field, url in (
			("webhook_url", "http://127.0.0.1:8000/hook"),
			("redirect_url", "http://shop.example.test/complete"),
			("redirect_url", "https://user:pass@shop.example.test/complete"),
			("redirect_url", "https://shop.example.test/complete#token"),
			("redirect_url", "https://shop.example.test\\@evil.test/complete"),
			("redirect_url", "https://[not-an-ipv6-address]/complete"),
		):
			with self.subTest(field=field, url=url):
				with self.assertRaises(RecurringPaymentValidationError):
					begin_first_payment("Fake Gateway", first_payment_request(**{field: url}))

	def test_accepts_authorized_as_a_lossless_payment_status(self):
		controller = self.controller_for(
			begin_first_payment=lambda request: payment_snapshot(status="authorized")
		)
		result = self.dispatch_with(controller, begin_first_payment, "Fake Gateway", first_payment_request())
		self.assertEqual(result["status"], "authorized")

	def test_rejects_provider_results_that_change_correlated_values_or_add_fields(self):
		for result in (
			payment_snapshot(amount="99.00"),
			payment_snapshot(customer_reference="another-customer"),
			payment_snapshot(secret_provider_payload={"should": "not leak"}),
			payment_snapshot(status="provider-private"),
		):
			with self.subTest(result=result):
				controller = self.controller_for(begin_first_payment=lambda request, result=result: result)
				with self.assertRaises(RecurringPaymentValidationError):
					self.dispatch_with(
						controller, begin_first_payment, "Fake Gateway", first_payment_request()
					)


class TestSubscriptionOperations(RecurringContractTestCase):
	def test_activation_requires_the_caller_to_supply_an_authoritative_valid_mandate(self):
		for status in ("pending", "invalid", "revoked", "expired"):
			with self.subTest(status=status):
				with patch("payments.recurring.get_payment_gateway_controller") as resolver:
					with self.assertRaisesRegex(RecurringPaymentValidationError, "authoritative"):
						activate_subscription("Fake Gateway", subscription_request(mandate_status=status))
				resolver.assert_not_called()

	def test_activation_normalizes_schedule_and_returns_a_correlated_snapshot(self):
		controller = self.controller_for(activate_subscription=lambda request: subscription_snapshot())
		result = self.dispatch_with(controller, activate_subscription, "Fake Gateway", subscription_request())
		request = controller.activate_subscription.call_args.args[0]
		self.assertEqual(request["amount"], "19.00")
		self.assertEqual(request["start_date"], "2026-07-01")
		self.assertEqual(result["start_date"], request["start_date"])
		self.assertEqual(result["description"], request["description"])
		self.assertEqual(result["provider_subscription_id"], "subscription-provider/1")

	def test_activation_round_trips_optional_finite_payment_count(self):
		controller = self.controller_for(
			activate_subscription=lambda request: subscription_snapshot(payment_count=12)
		)
		result = self.dispatch_with(
			controller,
			activate_subscription,
			"Fake Gateway",
			subscription_request(payment_count=12),
		)
		self.assertEqual(result["payment_count"], 12)

		for payment_count in (0, -1, 1.5, True, "12"):
			with self.subTest(payment_count=payment_count):
				with self.assertRaises(RecurringPaymentValidationError):
					activate_subscription("Fake Gateway", subscription_request(payment_count=payment_count))

		omitting = self.controller_for(activate_subscription=lambda request: subscription_snapshot())
		with self.assertRaisesRegex(RecurringPaymentValidationError, "payment_count"):
			self.dispatch_with(
				omitting,
				activate_subscription,
				"Fake Gateway",
				subscription_request(payment_count=12),
			)

		adding = self.controller_for(
			activate_subscription=lambda request: subscription_snapshot(payment_count=12)
		)
		with self.assertRaisesRegex(RecurringPaymentValidationError, "payment_count"):
			self.dispatch_with(adding, activate_subscription, "Fake Gateway", subscription_request())

	def test_activation_rejects_changed_schedule_fields(self):
		for result in (
			subscription_snapshot(start_date="2026-07-02"),
			subscription_snapshot(description="Changed description"),
			subscription_snapshot(payment_count=13),
		):
			with self.subTest(result=result):
				controller = self.controller_for(activate_subscription=lambda request, result=result: result)
				request = subscription_request(**({"payment_count": 12} if "payment_count" in result else {}))
				with self.assertRaises(RecurringPaymentValidationError):
					self.dispatch_with(controller, activate_subscription, "Fake Gateway", request)

	def test_activation_rejects_nonportable_intervals_dates_and_naive_result_timestamps(self):
		controller = self.controller_for(activate_subscription=lambda request: subscription_snapshot())
		for field, value in (
			("interval", "monthly"),
			("interval", "P1M2D"),
			("interval", "P0M"),
			("start_date", "07/01/2026"),
			("start_date", "2026-02-30"),
		):
			with self.subTest(field=field, value=value):
				with self.assertRaises(RecurringPaymentValidationError):
					activate_subscription("Fake Gateway", subscription_request(**{field: value}))

		controller = self.controller_for(
			activate_subscription=lambda request: subscription_snapshot(next_payment_at="2026-08-01T00:00:00")
		)
		with self.assertRaises(RecurringPaymentValidationError):
			self.dispatch_with(controller, activate_subscription, "Fake Gateway", subscription_request())

	def test_accepts_pending_as_a_lossless_subscription_status(self):
		controller = self.controller_for(
			activate_subscription=lambda request: subscription_snapshot(status="pending")
		)
		result = self.dispatch_with(controller, activate_subscription, "Fake Gateway", subscription_request())
		self.assertEqual(result["status"], "pending")

	def test_cancellation_requires_customer_scope_and_an_idempotency_key(self):
		controller = self.controller_for(
			cancel_subscription=lambda request: subscription_snapshot(status="canceled")
		)
		result = self.dispatch_with(controller, cancel_subscription, "Fake Gateway", cancellation_request())
		self.assertEqual(result["status"], "canceled")
		normalized_request = controller.cancel_subscription.call_args.args[0]
		self.assertEqual(normalized_request["idempotency_key"], "cancel/agreement:42")
		self.assertEqual(normalized_request["provider_customer_id"], "customer-provider/1")

		missing_key = cancellation_request()
		del missing_key["idempotency_key"]
		with self.assertRaises(RecurringPaymentValidationError):
			cancel_subscription("Fake Gateway", missing_key)

		missing_customer = cancellation_request()
		del missing_customer["provider_customer_id"]
		with self.assertRaises(RecurringPaymentValidationError):
			cancel_subscription("Fake Gateway", missing_customer)

		mismatch = self.controller_for(
			cancel_subscription=lambda request: subscription_snapshot(
				status="canceled", provider_customer_id="another-provider-customer"
			)
		)
		with self.assertRaisesRegex(RecurringPaymentValidationError, "provider_customer_id"):
			self.dispatch_with(mismatch, cancel_subscription, "Fake Gateway", cancellation_request())

	def test_reconciliation_is_read_only_and_requires_customer_scope(self):
		controller = self.controller_for(
			reconcile_subscription=lambda request: subscription_snapshot(status="suspended")
		)
		request = {
			"merchant_reference": "agreement/42",
			"customer_reference": "customer/opaque:α",
			"provider_customer_id": "customer-provider/1",
			"provider_subscription_id": "subscription-provider/1",
		}
		result = self.dispatch_with(
			controller, reconcile_subscription, "Fake Gateway", MappingProxyType(request)
		)
		self.assertEqual(result["status"], "suspended")
		normalized_request = controller.reconcile_subscription.call_args.args[0]
		self.assertEqual(normalized_request["provider_customer_id"], "customer-provider/1")
		self.assertNotIn("idempotency_key", normalized_request)

		missing_customer = dict(request)
		del missing_customer["provider_customer_id"]
		with self.assertRaises(RecurringPaymentValidationError):
			reconcile_subscription("Fake Gateway", missing_customer)

		mismatch = self.controller_for(
			reconcile_subscription=lambda normalized: subscription_snapshot(
				provider_customer_id="another-provider-customer"
			)
		)
		with self.assertRaisesRegex(RecurringPaymentValidationError, "provider_customer_id"):
			self.dispatch_with(mismatch, reconcile_subscription, "Fake Gateway", MappingProxyType(request))

	def test_retry_is_an_explicit_capability_and_never_falls_back_to_creating_a_charge(self):
		request = {
			"merchant_reference": "order/customer:42",
			"customer_reference": "customer/opaque:α",
			"provider_payment_id": "pay/provider:1",
			"idempotency_key": "retry/pay:1",
		}
		controller = object()
		with patch("payments.recurring.get_payment_gateway_controller", return_value=controller):
			with self.assertRaisesRegex(RecurringPaymentCapabilityError, "retry_payment"):
				retry_payment("Fake Gateway", request)

		capable = self.controller_for(retry_payment=lambda normalized: payment_snapshot(status="pending"))
		result = self.dispatch_with(capable, retry_payment, "Fake Gateway", request)
		self.assertEqual(result["status"], "pending")
		self.assertEqual(capable.retry_payment.call_count, 1)

	def test_all_missing_controller_operations_have_a_clear_capability_error(self):
		cases = (
			(begin_first_payment, first_payment_request()),
			(activate_subscription, subscription_request()),
			(cancel_subscription, cancellation_request()),
			(
				reconcile_subscription,
				{
					"merchant_reference": "agreement/42",
					"customer_reference": "customer/opaque:α",
					"provider_customer_id": "customer-provider/1",
					"provider_subscription_id": "subscription-provider/1",
				},
			),
			(normalize_provider_events, {"id": "provider-event/1"}),
		)
		for callable, request in cases:
			with self.subTest(operation=callable.__name__):
				with patch("payments.recurring.get_payment_gateway_controller", return_value=object()):
					with self.assertRaisesRegex(RecurringPaymentCapabilityError, callable.__name__):
						callable("Fake Gateway", request)


class TestProviderEvents(RecurringContractTestCase):
	def test_normalizes_multiple_provider_events_and_injects_the_resolved_gateway(self):
		provider_body = {"id": "provider-event/1", "nested": {"status": "paid"}}
		returned_event = recurring_event()
		returned_event.pop("payment_gateway")

		def normalize(payload):
			self.assertIsInstance(payload, FrozenDict)
			with self.assertRaises(TypeError):
				payload["nested"]["status"] = "forged"
			return [returned_event]

		controller = self.controller_for(normalize_provider_events=normalize)
		events = self.dispatch_with(controller, normalize_provider_events, "Fake Gateway", provider_body)
		self.assertIsInstance(events, list)
		self.assertIsInstance(events[0], FrozenDict)
		self.assertEqual(events[0]["payment_gateway"], "Fake Gateway")
		self.assertEqual(events[0]["payment"]["status"], "paid")
		self.assertEqual(provider_body["nested"]["status"], "paid")

	def test_rejects_event_gateway_spoofing_missing_snapshots_and_wrong_event_shapes(self):
		invalid_events = (
			recurring_event(payment_gateway="Another Gateway"),
			{
				"event_id": "event/2",
				"event_type": "mandate.updated",
				"merchant_reference": "order/customer:42",
				"customer_reference": "customer/opaque:α",
				"payment": payment_snapshot(),
			},
			recurring_event(provider_payload={"raw": "forbidden"}),
		)
		for event in invalid_events:
			with self.subTest(event=event):
				controller = self.controller_for(
					normalize_provider_events=lambda payload, event=event: [event]
				)
				with self.assertRaises(RecurringPaymentValidationError):
					self.dispatch_with(controller, normalize_provider_events, "Fake Gateway", {"id": "hint"})

	def test_rejects_mismatched_shared_provider_ids_across_event_snapshots(self):
		mandate = {
			"provider_mandate_id": "mandate-provider/1",
			"status": "valid",
			"merchant_reference": "order/customer:42",
			"customer_reference": "customer/opaque:α",
			"provider_customer_id": "customer-provider/1",
		}
		subscription = subscription_snapshot(
			merchant_reference="order/customer:42",
			provider_subscription_id="subscription-provider/1",
		)
		cases = (
			(
				"provider_customer_id",
				recurring_event(mandate={**mandate, "provider_customer_id": "another-provider-customer"}),
			),
			(
				"provider_mandate_id",
				recurring_event(
					payment=payment_snapshot(provider_mandate_id="another-provider-mandate"),
					mandate=mandate,
				),
			),
			(
				"provider_subscription_id",
				recurring_event(
					payment=payment_snapshot(provider_subscription_id="another-provider-subscription"),
					subscription=subscription,
				),
			),
		)
		for field, event in cases:
			with self.subTest(field=field):
				controller = self.controller_for(normalize_provider_events=lambda payload: [event])
				with self.assertRaisesRegex(RecurringPaymentValidationError, field):
					self.dispatch_with(controller, normalize_provider_events, "Fake Gateway", {"id": "hint"})

	def test_rejects_unbounded_or_non_json_provider_payloads_before_dispatch(self):
		deep = {"value": 1}
		for _ in range(14):
			deep = {"nested": deep}
		invalid = (
			["provider event must be a mapping"],
			{"blob": "x" * 70000},
			{"not_json": Decimal("1.00")},
			{"nan": float("nan")},
			deep,
		)
		for payload in invalid:
			with self.subTest(payload_type=type(payload).__name__):
				with patch("payments.recurring.get_payment_gateway_controller") as resolver:
					with self.assertRaises(RecurringPaymentValidationError):
						normalize_provider_events("Fake Gateway", payload)
				resolver.assert_not_called()

	def test_requires_provider_normalizers_to_return_a_list_of_exact_events(self):
		for returned in (None, recurring_event(), (recurring_event(),), ["not-a-mapping"]):
			with self.subTest(returned=returned):
				controller = self.controller_for(
					normalize_provider_events=lambda payload, returned=returned: returned
				)
				with self.assertRaises(RecurringPaymentValidationError):
					self.dispatch_with(controller, normalize_provider_events, "Fake Gateway", {"id": "hint"})


class TestEventDelivery(unittest.TestCase):
	def test_emits_one_immutable_normalized_event_through_the_generic_hook(self):
		input_event = recurring_event()
		with patch("payments.recurring.call_hook_method") as hook:
			normalized = emit_recurring_event(input_event)

		hook.assert_called_once_with("recurring_payment_event", event=normalized)
		self.assertEqual(normalized["contract_version"], CONTRACT_VERSION)
		self.assertEqual(normalized["event_id"], "event/provider:1/paid")
		self.assertIsNot(normalized["payment"], input_event["payment"])
		with self.assertRaises(TypeError):
			normalized["payment"]["status"] = "failed"

	def test_invalid_events_are_not_delivered(self):
		with patch("payments.recurring.call_hook_method") as hook:
			with self.assertRaises(RecurringPaymentValidationError):
				emit_recurring_event(recurring_event(event_type="provider.private.event"))
		hook.assert_not_called()

	def test_delivery_errors_propagate_for_at_least_once_retry_by_the_transport(self):
		with patch("payments.recurring.call_hook_method", side_effect=RuntimeError("consumer failed")):
			with self.assertRaisesRegex(RuntimeError, "consumer failed"):
				emit_recurring_event(recurring_event())


class TestMetadataBounds(RecurringContractTestCase):
	def test_metadata_is_bounded_json_and_frozen_recursively(self):
		controller = self.controller_for(begin_first_payment=lambda request: payment_snapshot())
		request = first_payment_request(metadata={"nested": [{"value": 1.25}]})
		self.dispatch_with(controller, begin_first_payment, "Fake Gateway", request)
		metadata = controller.begin_first_payment.call_args.args[0]["metadata"]
		self.assertEqual(json.loads(json.dumps(metadata)), request["metadata"])
		with self.assertRaises(TypeError):
			metadata["nested"].append("changed")

	def test_metadata_rejects_excess_keys_depth_size_and_non_json_values(self):
		invalid_metadata = (
			["metadata must be a mapping"],
			"metadata must be a mapping",
			{f"key-{index}": index for index in range(33)},
			{"a": {"b": {"c": {"d": {"e": 1}}}}},
			{"blob": "x" * 5000},
			{"decimal": Decimal("1.00")},
			{"bad": float("inf")},
		)
		for metadata in invalid_metadata:
			with self.subTest(metadata=metadata):
				with self.assertRaises(RecurringPaymentValidationError):
					begin_first_payment("Fake Gateway", first_payment_request(metadata=metadata))


if __name__ == "__main__":
	unittest.main()
