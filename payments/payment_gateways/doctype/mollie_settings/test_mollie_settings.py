# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE

import unittest
from unittest.mock import MagicMock, patch

import frappe

from payments.payment_gateways.doctype.mollie_settings.mollie_settings import (
	MOLLIE_API_BASE,
	MollieMandatePending,
	MollieResourceNotFound,
	MollieSettings,
	_derived_idempotency_key,
	_from_mollie_interval,
	_payment_snapshot,
	_subscription_snapshot,
	_to_mollie_interval,
)
from payments.recurring import (
	RecurringPaymentCapabilityError,
	RecurringPaymentValidationError,
	activate_subscription,
	begin_first_payment,
	normalize_provider_events,
)

REFERENCES = {
	"payments_contract_version": 1,
	"merchant_reference": "agreement/42",
	"customer_reference": "customer/42",
}


def payment(**overrides):
	value = {
		"id": "tr_payment1",
		"status": "open",
		"amount": {"value": "12.50", "currency": "EUR"},
		"customerId": "cst_customer1",
		"sequenceType": "first",
		"metadata": dict(REFERENCES),
		"createdAt": "2026-08-24T12:00:00+00:00",
		"_links": {"checkout": {"href": "https://www.mollie.com/checkout/test"}},
	}
	value.update(overrides)
	return value


def customer(**overrides):
	value = {
		"id": "cst_customer1",
		"metadata": {
			"payments_contract_version": 1,
			"customer_reference": "customer/42",
		},
	}
	value.update(overrides)
	return value


def subscription(**overrides):
	value = {
		"id": "sub_subscription1",
		"status": "active",
		"amount": {"value": "19.00", "currency": "EUR"},
		"customerId": "cst_customer1",
		"mandateId": "mdt_mandate1",
		"interval": "1 month",
		"startDate": "2026-09-01",
		"description": "Monthly access",
		"metadata": {
			**REFERENCES,
			"provider_mandate_id": "mdt_mandate1",
		},
		"createdAt": "2026-08-24T12:05:00+00:00",
	}
	value.update(overrides)
	return value


def mandate(**overrides):
	value = {
		"id": "mdt_mandate1",
		"status": "valid",
		"customerId": "cst_customer1",
		"createdAt": "2026-08-24T12:04:00+00:00",
	}
	value.update(overrides)
	return value


def first_request(**overrides):
	value = {
		"merchant_reference": "agreement/42",
		"customer_reference": "customer/42",
		"customer_name": "Ada Example",
		"customer_email": "ada@example.test",
		"customer_locale": "en_GB",
		"amount": "12.50",
		"currency": "EUR",
		"description": "First payment",
		"redirect_url": "https://shop.example.test/complete",
		"webhook_url": "https://shop.example.test/mollie-hook",
		"idempotency_key": "first/agreement/42",
	}
	value.update(overrides)
	return value


def subscription_request(**overrides):
	value = {
		"merchant_reference": "agreement/42",
		"customer_reference": "customer/42",
		"provider_customer_id": "cst_customer1",
		"provider_mandate_id": "mdt_mandate1",
		"mandate_status": "valid",
		"amount": "19.00",
		"currency": "EUR",
		"interval": "P1M",
		"start_date": "2026-09-01",
		"description": "Monthly access",
		"webhook_url": "https://shop.example.test/mollie-hook",
		"idempotency_key": "activate/agreement/42",
	}
	value.update(overrides)
	return value


def cancel_request(**overrides):
	value = {
		"merchant_reference": "agreement/42",
		"customer_reference": "customer/42",
		"provider_customer_id": "cst_customer1",
		"provider_subscription_id": "sub_subscription1",
		"idempotency_key": "cancel/agreement/42",
	}
	value.update(overrides)
	return value


def settings():
	controller = MollieSettings.__new__(MollieSettings)
	controller.__dict__.update(
		{
			"doctype": "Mollie Settings",
			"name": "Primary",
			"gateway_name": "Primary",
			"enabled": 1,
			"api_key": "test_api_key",
			"webhook_token": "route-token",
		}
	)
	controller.get_password = MagicMock(
		side_effect=lambda fieldname, raise_exception=False: {
			"api_key": "test_api_key",
			"webhook_token": "route-token",
		}[fieldname]
	)
	controller.get_recurring_webhook_url = MagicMock(return_value="https://shop.example.test/mollie-hook")
	return controller


class TestMollieSettingsLifecycle(unittest.TestCase):
	def test_rejects_non_mollie_api_key_prefixes(self):
		controller = settings()
		for api_key in ("", "sk_test_other_provider", None):
			controller.api_key = api_key
			with self.subTest(api_key=api_key), self.assertRaises(frappe.ValidationError):
				controller.validate()
		controller.api_key = "live_valid_key"
		controller.validate()

	def test_generates_routing_token_and_registers_named_gateway(self):
		controller = settings()
		controller.__dict__["webhook_token"] = None
		with patch(
			"payments.payment_gateways.doctype.mollie_settings.mollie_settings.frappe.generate_hash",
			return_value="generated-route-token",
		):
			controller.before_insert()
		self.assertEqual(controller.webhook_token, "generated-route-token")
		with (
			patch(
				"payments.payment_gateways.doctype.mollie_settings.mollie_settings.create_payment_gateway"
			) as create_gateway,
			patch(
				"payments.payment_gateways.doctype.mollie_settings.mollie_settings.call_hook_method"
			) as hook,
		):
			controller.on_update()
		create_gateway.assert_called_once_with(
			"Mollie-Primary", settings="Mollie Settings", controller="Primary"
		)
		hook.assert_called_once_with("payment_gateway_enabled", gateway="Mollie-Primary")

	def test_webhook_url_is_account_scoped_and_does_not_expose_api_key(self):
		controller = settings()
		with patch(
			"payments.payment_gateways.doctype.mollie_settings.mollie_settings.get_url",
			return_value="https://site.example.test/api/method/mollie_webhook",
		):
			url = MollieSettings.get_recurring_webhook_url(controller)
		self.assertIn("gateway=Mollie-Primary", url)
		self.assertIn("token=route-token", url)
		self.assertNotIn("test_api_key", url)
		self.assertTrue(controller.webhook_token_matches("route-token"))
		self.assertFalse(controller.webhook_token_matches("forged"))
		self.assertFalse(controller.webhook_token_matches(None))


class TestMollieFirstPayment(unittest.TestCase):
	def test_creates_customer_then_first_payment_with_separate_idempotency_keys(self):
		controller = settings()
		controller._post = MagicMock(side_effect=[{"id": "cst_customer1"}, payment()])
		result = controller.begin_first_payment(first_request())

		customer_call, payment_call = controller._post.call_args_list
		self.assertEqual(customer_call.args[0], "/customers")
		self.assertEqual(customer_call.args[1]["name"], "Ada Example")
		self.assertEqual(customer_call.args[1]["email"], "ada@example.test")
		self.assertEqual(customer_call.args[1]["metadata"]["customer_reference"], "customer/42")
		self.assertNotIn("merchant_reference", customer_call.args[1]["metadata"])
		self.assertEqual(payment_call.args[0], "/payments")
		self.assertEqual(payment_call.args[1]["sequenceType"], "first")
		self.assertEqual(payment_call.args[1]["customerId"], "cst_customer1")
		self.assertEqual(payment_call.args[1]["amount"], {"value": "12.50", "currency": "EUR"})
		self.assertNotEqual(customer_call.kwargs["idempotency_key"], payment_call.kwargs["idempotency_key"])
		self.assertEqual(result["checkout_url"], "https://www.mollie.com/checkout/test")

	def test_reuses_only_an_authoritatively_correlated_customer(self):
		controller = settings()
		controller._get = MagicMock(return_value=customer())
		controller._post = MagicMock(return_value=payment())
		request = first_request(provider_customer_id="cst_customer1")
		for field in ("customer_name", "customer_email", "customer_locale"):
			request.pop(field)
		controller.begin_first_payment(request)
		controller._get.assert_called_once_with("/customers/cst_customer1")
		controller._post.assert_called_once()
		self.assertEqual(controller._post.call_args.args[1]["customerId"], "cst_customer1")

		for wrong_customer in (
			customer(metadata={"payments_contract_version": 1, "customer_reference": "other"}),
			customer(id="cst_other"),
			customer(metadata={"customer_reference": "customer/42"}),
		):
			with self.subTest(customer=wrong_customer):
				controller._get.return_value = wrong_customer
				with self.assertRaises(RecurringPaymentValidationError):
					controller.begin_first_payment(request)

	def test_rejects_a_webhook_url_for_another_account_before_remote_mutation(self):
		controller = settings()
		controller._post = MagicMock()
		with self.assertRaisesRegex(RecurringPaymentValidationError, "selected payment gateway"):
			controller.begin_first_payment(
				first_request(webhook_url="https://site.example.test/other-account")
			)
		controller._post.assert_not_called()

	def test_rejects_a_payment_returned_for_another_customer_or_reference(self):
		for changed in (
			payment(customerId="cst_other"),
			payment(metadata={**REFERENCES, "merchant_reference": "another"}),
		):
			with self.subTest(changed=changed):
				controller = settings()
				controller._post = MagicMock(side_effect=[{"id": "cst_customer1"}, changed])
				with self.assertRaises(RecurringPaymentValidationError):
					controller.begin_first_payment(first_request())


class TestMollieSubscriptionOperations(unittest.TestCase):
	def setUp(self):
		self.cache_values = {}
		cache = MagicMock()
		cache.get_value.side_effect = self.cache_values.get
		cache.set_value.side_effect = lambda key, value, **kwargs: self.cache_values.__setitem__(key, value)
		patcher = patch.object(frappe, "cache", return_value=cache)
		patcher.start()
		self.addCleanup(patcher.stop)

	def test_activation_translates_interval_and_optional_finite_count(self):
		controller = settings()
		controller._post = MagicMock(return_value=subscription(times=12))
		result = controller.activate_subscription(subscription_request(payment_count=12))
		path, payload = controller._post.call_args.args
		self.assertEqual(path, "/customers/cst_customer1/subscriptions")
		self.assertEqual(payload["interval"], "1 month")
		self.assertEqual(payload["times"], 12)
		self.assertEqual(payload["mandateId"], "mdt_mandate1")
		self.assertEqual(result["interval"], "P1M")
		self.assertEqual(result["payment_count"], 12)

	def test_activation_rejects_a_cross_account_webhook_before_remote_mutation(self):
		controller = settings()
		controller._post = MagicMock()
		with self.assertRaisesRegex(RecurringPaymentValidationError, "selected payment gateway"):
			controller.activate_subscription(
				subscription_request(webhook_url="https://site.example.test/other-account")
			)
		controller._post.assert_not_called()

	def test_activation_refuses_a_non_valid_mandate_even_when_called_directly(self):
		controller = settings()
		controller._post = MagicMock()
		with self.assertRaisesRegex(RecurringPaymentValidationError, "valid mandate"):
			controller.activate_subscription(subscription_request(mandate_status="pending"))
		controller._post.assert_not_called()

	def test_cancellation_gets_before_delete_and_reconciles(self):
		controller = settings()
		controller._get = MagicMock(side_effect=[subscription(), subscription(status="canceled")])
		controller._delete = MagicMock(return_value=None)
		result = controller.cancel_subscription(cancel_request())
		self.assertEqual(result["status"], "canceled")
		self.assertEqual(
			controller._delete.call_args.args[0],
			"/customers/cst_customer1/subscriptions/sub_subscription1",
		)
		self.assertEqual(controller._get.call_count, 2)

	def test_unknown_subscription_is_not_treated_as_idempotent_success(self):
		controller = settings()
		controller._get = MagicMock(side_effect=MollieResourceNotFound("unknown"))
		controller._delete = MagicMock()
		with self.assertRaises(MollieResourceNotFound):
			controller.cancel_subscription(cancel_request())
		controller._delete.assert_not_called()

	def test_known_post_delete_404_is_repeatably_canceled_for_only_the_exact_operation(self):
		controller = settings()
		controller._get = MagicMock(side_effect=[subscription(), MollieResourceNotFound("gone")])
		controller._delete = MagicMock(return_value=None)
		request = cancel_request()
		result = controller.cancel_subscription(request)
		self.assertEqual(result["status"], "canceled")

		controller._get.reset_mock()
		controller._delete.reset_mock()
		self.assertEqual(controller.cancel_subscription(request)["status"], "canceled")
		controller._get.assert_not_called()
		controller._delete.assert_not_called()

		controller._get.side_effect = MollieResourceNotFound("unknown under another key")
		with self.assertRaises(MollieResourceNotFound):
			controller.cancel_subscription(cancel_request(idempotency_key="different-operation"))

	def test_concurrent_delete_404_after_known_get_is_canceled_success(self):
		controller = settings()
		controller._get = MagicMock(return_value=subscription())
		controller._delete = MagicMock(side_effect=MollieResourceNotFound("already gone"))
		result = controller.cancel_subscription(cancel_request())
		self.assertEqual(result["status"], "canceled")
		controller._get.assert_called_once()

	def test_already_canceled_does_not_delete_again(self):
		controller = settings()
		controller._get = MagicMock(return_value=subscription(status="canceled"))
		controller._delete = MagicMock()
		result = controller.cancel_subscription(cancel_request())
		self.assertEqual(result["status"], "canceled")
		controller._delete.assert_not_called()

	def test_reconcile_is_customer_scoped_and_rejects_reference_mismatch(self):
		controller = settings()
		controller._get = MagicMock(return_value=subscription())
		result = controller.reconcile_subscription(cancel_request())
		self.assertEqual(result["provider_subscription_id"], "sub_subscription1")
		controller._get.assert_called_once_with("/customers/cst_customer1/subscriptions/sub_subscription1")
		controller._get.return_value = subscription(
			metadata={**REFERENCES, "merchant_reference": "another", "provider_mandate_id": "mdt_mandate1"}
		)
		with self.assertRaises(RecurringPaymentValidationError):
			controller.reconcile_subscription(cancel_request())

	def test_customer_scoped_paths_reject_non_mollie_ids_before_http(self):
		controller = settings()
		controller._get = MagicMock()
		controller._post = MagicMock()
		controller._delete = MagicMock()
		with self.assertRaises(RecurringPaymentValidationError):
			controller.reconcile_subscription(cancel_request(provider_customer_id="../../customers"))
		with self.assertRaises(RecurringPaymentValidationError):
			controller.cancel_subscription(
				cancel_request(provider_subscription_id="sub_valid/../../payments")
			)
		with self.assertRaises(RecurringPaymentValidationError):
			controller.activate_subscription(subscription_request(provider_mandate_id="mandate-not-mollie"))
		controller._get.assert_not_called()
		controller._post.assert_not_called()
		controller._delete.assert_not_called()

	def test_retry_is_explicitly_unsupported_and_never_calls_the_api(self):
		controller = settings()
		controller._request = MagicMock()
		with self.assertRaises(RecurringPaymentCapabilityError):
			controller.retry_payment({"provider_payment_id": "tr_payment1"})
		controller._request.assert_not_called()


class TestMollieNormalization(unittest.TestCase):
	def test_first_payment_and_mandate_events_have_transition_stable_ids(self):
		controller = settings()
		provider_event = {
			"payment": payment(status="paid", paidAt="2026-08-24T12:03:00+00:00"),
			"mandate": mandate(),
		}
		first = controller.normalize_provider_events(provider_event)
		redelivery = controller.normalize_provider_events(provider_event)
		self.assertEqual([event["event_id"] for event in first], [event["event_id"] for event in redelivery])
		self.assertEqual(
			[event["event_type"] for event in first], ["first_payment.updated", "mandate.updated"]
		)
		changed = controller.normalize_provider_events(
			{"payment": provider_event["payment"], "mandate": mandate(status="revoked")}
		)
		self.assertEqual(first[0]["event_id"], changed[0]["event_id"])
		self.assertNotEqual(first[1]["event_id"], changed[1]["event_id"])

	def test_recurring_payment_carries_authoritative_subscription_events(self):
		controller = settings()
		provider_payment = payment(
			id="tr_recurring1",
			status="pending",
			sequenceType="recurring",
			subscriptionId="sub_subscription1",
			mandateId="mdt_mandate1",
		)
		events = controller.normalize_provider_events(
			{"payment": provider_payment, "subscription": subscription(status="suspended")}
		)
		self.assertEqual(
			[event["event_type"] for event in events],
			["recurring_payment.updated", "subscription.updated"],
		)
		self.assertEqual(events[0]["subscription"]["status"], "suspended")

	def test_authorized_refund_and_chargeback_states_are_lossless(self):
		self.assertEqual(_payment_snapshot(payment(status="authorized"))["status"], "authorized")
		for provider_fields, expected_status, amount_field, expected_amount in (
			(
				{"amountRefunded": {"value": "2.50", "currency": "EUR"}},
				"partially_refunded",
				"refunded_amount",
				"2.50",
			),
			(
				{"amountRefunded": {"value": "12.50", "currency": "EUR"}},
				"refunded",
				"refunded_amount",
				"12.50",
			),
			(
				{"amountChargedBack": {"value": "3.00", "currency": "EUR"}},
				"partially_charged_back",
				"charged_back_amount",
				"3.00",
			),
			(
				{"amountChargedBack": {"value": "12.50", "currency": "EUR"}},
				"charged_back",
				"charged_back_amount",
				"12.50",
			),
		):
			with self.subTest(status=expected_status):
				snapshot = _payment_snapshot(payment(status="paid", **provider_fields))
				self.assertEqual(snapshot["status"], expected_status)
				self.assertEqual(snapshot[amount_field], expected_amount)
				self.assertNotIn("occurred_at", snapshot)

		for malformed in (
			payment(amountChargedBack={"value": "2.50", "currency": "USD"}),
			payment(amountChargedBack={"value": "13.00", "currency": "EUR"}),
			payment(
				amountRefunded={"value": "8.00", "currency": "EUR"},
				amountChargedBack={"value": "8.00", "currency": "EUR"},
			),
		):
			with self.subTest(payment=malformed), self.assertRaises(RecurringPaymentValidationError):
				_payment_snapshot(malformed)

	def test_payment_event_identity_changes_for_its_own_reversal_amount_only(self):
		controller = settings()
		base = {"payment": payment(status="paid", paidAt="2026-08-24T12:03:00+00:00")}
		paid = controller.normalize_provider_events(base)[0]
		partial = controller.normalize_provider_events(
			{
				"payment": payment(
					status="paid",
					amountRefunded={"value": "2.50", "currency": "EUR"},
				)
			}
		)[0]
		more = controller.normalize_provider_events(
			{
				"payment": payment(
					status="paid",
					amountRefunded={"value": "3.50", "currency": "EUR"},
				)
			}
		)[0]
		self.assertNotEqual(paid["event_id"], partial["event_id"])
		self.assertNotEqual(partial["event_id"], more["event_id"])

	def test_cross_resource_customer_or_reference_mismatch_is_rejected(self):
		controller = settings()
		for bad_subscription in (
			subscription(customerId="cst_other"),
			subscription(
				metadata={**REFERENCES, "customer_reference": "other", "provider_mandate_id": "mdt_mandate1"}
			),
		):
			with self.subTest(subscription=bad_subscription):
				with self.assertRaises(RecurringPaymentValidationError):
					controller.normalize_provider_events(
						{
							"payment": payment(
								sequenceType="recurring",
								subscriptionId="sub_subscription1",
								mandateId="mdt_mandate1",
							),
							"subscription": bad_subscription,
						}
					)

	def test_unexpected_resource_and_oneoff_payment_are_rejected(self):
		controller = settings()
		with self.assertRaises(RecurringPaymentValidationError):
			controller.normalize_provider_events({"payment": payment(), "posted_status": "paid"})
		with self.assertRaises(RecurringPaymentValidationError):
			controller.normalize_provider_events({"payment": payment(sequenceType="oneoff")})

	def test_interval_translation_is_exact_and_round_trips(self):
		for portable, mollie in (
			("P1D", "1 day"),
			("P2W", "2 weeks"),
			("P1M", "1 month"),
			("P3Y", "3 years"),
		):
			with self.subTest(portable=portable):
				self.assertEqual(_to_mollie_interval(portable), mollie)
				self.assertEqual(_from_mollie_interval(mollie), portable)
		for invalid in ("P0M", "PT1H", "1 month"):
			with self.assertRaises(RecurringPaymentValidationError):
				_to_mollie_interval(invalid)

	def test_canceled_subscription_uses_cancellation_not_creation_timestamp(self):
		snapshot = _subscription_snapshot(
			subscription(status="canceled", canceledAt="2026-09-05T12:00:00+00:00")
		)
		self.assertEqual(snapshot["occurred_at"], "2026-09-05T12:00:00+00:00")

	def test_subscription_snapshot_requires_full_remote_schedule(self):
		for field in ("interval", "startDate", "description", "mandateId"):
			resource = subscription()
			resource.pop(field)
			if field == "mandateId":
				resource["metadata"].pop("provider_mandate_id")
			with self.subTest(field=field):
				with self.assertRaises(RecurringPaymentValidationError):
					_subscription_snapshot(resource)


class TestMolliePublicContract(unittest.TestCase):
	def test_first_payment_round_trips_through_provider_neutral_dispatch(self):
		controller = settings()
		controller._post = MagicMock(side_effect=[{"id": "cst_customer1"}, payment()])
		with patch("payments.recurring.get_payment_gateway_controller", return_value=controller):
			result = begin_first_payment("Mollie-Primary", first_request())
		self.assertEqual(result["provider_payment_id"], "tr_payment1")
		self.assertEqual(result["amount"], "12.50")

	def test_subscription_round_trips_through_provider_neutral_dispatch(self):
		controller = settings()
		controller._post = MagicMock(return_value=subscription(times=6))
		with patch("payments.recurring.get_payment_gateway_controller", return_value=controller):
			result = activate_subscription("Mollie-Primary", subscription_request(payment_count=6))
		self.assertEqual(result["interval"], "P1M")
		self.assertEqual(result["payment_count"], 6)

	def test_events_round_trip_through_provider_neutral_validation(self):
		controller = settings()
		provider_event = {
			"payment": payment(status="paid", paidAt="2026-08-24T12:03:00+00:00"),
			"mandate": mandate(),
		}
		with patch("payments.recurring.get_payment_gateway_controller", return_value=controller):
			events = normalize_provider_events("Mollie-Primary", provider_event)
		self.assertEqual(len(events), 2)
		self.assertEqual(events[0]["payment_gateway"], "Mollie-Primary")
		self.assertEqual(events[1]["mandate"]["status"], "valid")

		provider_event = {
			"payment": payment(
				status="paid",
				amountChargedBack={"value": "2.50", "currency": "EUR"},
			)
		}
		with patch("payments.recurring.get_payment_gateway_controller", return_value=controller):
			events = normalize_provider_events("Mollie-Primary", provider_event)
		self.assertEqual(events[0]["payment"]["status"], "partially_charged_back")
		self.assertEqual(events[0]["payment"]["charged_back_amount"], "2.50")

	def test_fallback_valid_mandate_normalizes_as_a_separate_resource_event(self):
		controller = settings()
		provider_event = {
			"payment": payment(
				status="paid",
				mandateId="mdt_pending1",
				paidAt="2026-08-24T12:03:00+00:00",
			),
			"mandate": mandate(id="mdt_valid1", status="valid"),
		}
		with patch("payments.recurring.get_payment_gateway_controller", return_value=controller):
			events = normalize_provider_events("Mollie-Primary", provider_event)

		self.assertEqual(events[0]["payment"]["provider_mandate_id"], "mdt_pending1")
		self.assertNotIn("mandate", events[0])
		self.assertEqual(events[1]["mandate"]["provider_mandate_id"], "mdt_valid1")
		self.assertEqual(events[1]["mandate"]["status"], "valid")


class TestMollieAuthoritativeFetchAndClient(unittest.TestCase):
	def test_paid_first_payment_fetches_and_selects_payment_mandate(self):
		controller = settings()
		paid = payment(
			status="paid",
			mandateId="mdt_selected1",
			paidAt="2026-08-24T12:03:00+00:00",
			webhookUrl="https://site.example.test/hook?token=secret",
			billingEmail="private@example.test",
		)
		controller._get = MagicMock(
			side_effect=[
				paid,
				{"_embedded": {"mandates": [mandate(id="mdt_other1"), mandate(id="mdt_selected1")]}},
			]
		)
		bundle = controller.fetch_authoritative_event("tr_payment1")
		self.assertEqual(bundle["mandate"]["id"], "mdt_selected1")
		self.assertNotIn("webhookUrl", bundle["payment"])
		self.assertNotIn("billingEmail", bundle["payment"])
		controller._get.assert_any_call("/customers/cst_customer1/mandates")

	def test_paid_first_payment_prefers_a_valid_mandate_and_retries_when_none_is_ready(self):
		controller = settings()
		paid = payment(
			status="paid",
			mandateId="mdt_pending1",
			paidAt="2026-08-24T12:03:00+00:00",
		)
		controller._get = MagicMock(
			side_effect=[
				paid,
				{
					"_embedded": {
						"mandates": [
							mandate(id="mdt_pending1", status="pending"),
							mandate(id="mdt_valid1", status="valid"),
						]
					}
				},
			]
		)
		self.assertEqual(controller.fetch_authoritative_event("tr_payment1")["mandate"]["id"], "mdt_valid1")

		for mandates in ([], [mandate(id="mdt_pending1", status="pending")]):
			with self.subTest(mandates=mandates):
				controller._get = MagicMock(side_effect=[paid, {"_embedded": {"mandates": mandates}}])
				with self.assertRaises(MollieMandatePending):
					controller.fetch_authoritative_event("tr_payment1")

	def test_recurring_payment_fetches_customer_scoped_subscription(self):
		controller = settings()
		controller._get = MagicMock(
			side_effect=[
				payment(
					id="tr_recurring1",
					sequenceType="recurring",
					subscriptionId="sub_subscription1",
					mandateId="mdt_mandate1",
				),
				subscription(),
			]
		)
		bundle = controller.fetch_authoritative_event("tr_recurring1")
		self.assertEqual(bundle["subscription"]["id"], "sub_subscription1")
		controller._get.assert_called_with("/customers/cst_customer1/subscriptions/sub_subscription1")

	def test_http_adapter_uses_timeout_bearer_and_deterministic_idempotency_header(self):
		controller = settings()
		key = _derived_idempotency_key("Primary", "merchant-key", "payment")
		response = MagicMock()
		response.json.return_value = payment()
		session = MagicMock()
		session.request.return_value = response
		with patch(
			"payments.payment_gateways.doctype.mollie_settings.mollie_settings.get_request_session",
			return_value=session,
		):
			controller._post("/payments", {"test": True}, idempotency_key=key)
		request = session.request
		self.assertEqual(request.call_args.args, ("POST", f"{MOLLIE_API_BASE}/payments"))
		self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer test_api_key")
		self.assertEqual(request.call_args.kwargs["headers"]["Idempotency-Key"], key)
		self.assertEqual(request.call_args.kwargs["json"], {"test": True})
		self.assertEqual(request.call_args.kwargs["timeout"], 30)
		response.raise_for_status.assert_called_once()
		self.assertEqual(key, _derived_idempotency_key("Primary", "merchant-key", "payment"))
		self.assertNotEqual(key, _derived_idempotency_key("Other", "merchant-key", "payment"))

	def test_http_adapter_accepts_an_empty_delete_response_without_json_parsing(self):
		controller = settings()
		response = MagicMock()
		session = MagicMock()
		session.request.return_value = response
		with patch(
			"payments.payment_gateways.doctype.mollie_settings.mollie_settings.get_request_session",
			return_value=session,
		):
			self.assertIsNone(
				controller._delete("/customers/cst_1/subscriptions/sub_1", idempotency_key="key")
			)
		self.assertEqual(session.request.call_args.args[0], "DELETE")
		self.assertEqual(session.request.call_args.kwargs["timeout"], 30)
		response.raise_for_status.assert_called_once()
		response.json.assert_not_called()

	def test_remote_404_is_classified_without_hiding_other_failures(self):
		controller = settings()
		not_found = RuntimeError("not found")
		not_found.status_code = 404
		session = MagicMock()
		session.request.side_effect = not_found
		with patch(
			"payments.payment_gateways.doctype.mollie_settings.mollie_settings.get_request_session",
			return_value=session,
		):
			with self.assertRaises(MollieResourceNotFound):
				controller._get("/payments/tr_unknown")
		session.request.side_effect = RuntimeError("network down")
		with patch(
			"payments.payment_gateways.doctype.mollie_settings.mollie_settings.get_request_session",
			return_value=session,
		):
			with self.assertRaisesRegex(RuntimeError, "network down"):
				controller._get("/payments/tr_payment1")


if __name__ == "__main__":
	unittest.main()
