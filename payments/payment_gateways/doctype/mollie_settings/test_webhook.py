# Copyright (c) 2026, Frappe Technologies and Contributors
# License: MIT. See LICENSE

import json
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from payments.payment_gateways.doctype.mollie_settings.mollie_settings import (
	MollieMandatePending,
	MollieResourceNotFound,
	MollieSettings,
)
from payments.payment_gateways.doctype.mollie_settings.test_mollie_settings import (
	mandate,
	payment,
	subscription,
)
from payments.payment_gateways.doctype.mollie_settings.webhook import (
	_MAX_DELIVERY_ATTEMPTS,
	_RECOVERY_BATCH_SIZE,
	MollieWebhookRejected,
	handle_recurring_notification,
	mollie_webhook,
	process_webhook,
	retry_failed_notifications,
)


class MollieWebhookTestCase(IntegrationTestCase):
	def setUp(self):
		suffix = frappe.generate_hash(length=8)
		self.settings = frappe.get_doc(
			{
				"doctype": "Mollie Settings",
				"gateway_name": f"Test-{suffix}",
				"enabled": 1,
				"api_key": "test_api_key",
			}
		).insert(ignore_permissions=True)
		self.gateway = self.settings.payment_gateway
		self.token = self.settings.get_password("webhook_token")

	def authoritative_first(self, status="paid", mandate_status="valid"):
		provider_payment = payment(
			status=status,
			paidAt="2026-08-24T12:03:00+00:00" if status == "paid" else None,
		)
		provider_payment.pop("paidAt", None) if status != "paid" else None
		return {"payment": provider_payment, "mandate": mandate(status=mandate_status)}

	def process(self, provider_event=None):
		provider_event = provider_event or self.authoritative_first()
		with patch.object(MollieSettings, "fetch_authoritative_event", return_value=provider_event):
			return process_webhook(self.gateway, self.token, "tr_payment1")


class TestMollieWebhookPersistence(MollieWebhookTestCase):
	def test_fetches_authoritatively_before_persisting_and_never_stores_posted_state(self):
		with patch.object(
			MollieSettings,
			"fetch_authoritative_event",
			return_value=self.authoritative_first(status="paid"),
		) as fetch:
			name, should_enqueue = process_webhook(self.gateway, self.token, "tr_payment1")
		fetch.assert_called_once_with("tr_payment1")
		self.assertTrue(should_enqueue)
		log = frappe.get_doc("Integration Request", name)
		data = json.loads(log.data)
		self.assertEqual(data["provider_event"]["payment"]["status"], "paid")
		self.assertEqual(data["provider_event"]["payment"]["amount"]["value"], "12.50")
		self.assertEqual(log.status, "Queued")

	def test_redelivery_reuses_transition_but_status_or_mandate_change_does_not(self):
		first_name, _ = self.process(self.authoritative_first(status="paid"))
		redelivery_name, _ = self.process(self.authoritative_first(status="paid"))
		failed_name, _ = self.process(self.authoritative_first(status="failed"))
		revoked_name, _ = self.process(self.authoritative_first(status="paid", mandate_status="revoked"))
		self.assertEqual(first_name, redelivery_name)
		self.assertNotEqual(first_name, failed_name)
		self.assertNotEqual(first_name, revoked_name)

	def test_completed_transition_is_not_requeued_but_failed_transition_is(self):
		name, _ = self.process()
		frappe.db.set_value("Integration Request", name, "status", "Completed")
		self.assertEqual(self.process(), (name, False))
		data = json.loads(frappe.db.get_value("Integration Request", name, "data"))
		data["delivery_attempts"] = 3
		frappe.db.set_value("Integration Request", name, {"status": "Failed", "data": json.dumps(data)})
		self.assertEqual(self.process(), (name, True))
		status, stored = frappe.db.get_value("Integration Request", name, ["status", "data"])
		self.assertEqual(status, "Queued")
		self.assertEqual(json.loads(stored)["delivery_attempts"], 3)

	def test_concurrent_deterministic_insert_reuses_the_winning_transition(self):
		name, _ = self.process()
		exists = frappe.db.exists

		def lose_exists_race(doctype, filters, *args, **kwargs):
			if doctype == "Integration Request":
				return False
			return exists(doctype, filters, *args, **kwargs)

		with patch.object(frappe.db, "exists", side_effect=lose_exists_race):
			redelivery_name, should_enqueue = self.process()
		self.assertEqual(redelivery_name, name)
		self.assertTrue(should_enqueue)
		self.assertEqual(frappe.db.count("Integration Request", {"name": name}), 1)

	def test_bad_route_token_and_unknown_payment_write_nothing(self):
		before = frappe.db.count("Integration Request")
		with patch.object(MollieSettings, "fetch_authoritative_event") as fetch:
			with self.assertRaises(MollieWebhookRejected):
				process_webhook(self.gateway, "forged", "tr_payment1")
		fetch.assert_not_called()
		with patch.object(
			MollieSettings,
			"fetch_authoritative_event",
			side_effect=MollieResourceNotFound("unknown"),
		):
			with self.assertRaises(MollieResourceNotFound):
				process_webhook(self.gateway, self.token, "tr_unknown")
		self.assertEqual(frappe.db.count("Integration Request"), before)

	def test_malformed_payment_id_is_rejected_before_remote_fetch(self):
		with patch.object(MollieSettings, "fetch_authoritative_event") as fetch:
			for value in ("", "../../customers", "tr_bad-status!", "sub_subscription1"):
				with self.subTest(value=value), self.assertRaises(MollieWebhookRejected):
					process_webhook(self.gateway, self.token, value)
		fetch.assert_not_called()

	def test_account_token_cannot_route_to_another_mollie_settings(self):
		other = frappe.get_doc(
			{
				"doctype": "Mollie Settings",
				"gateway_name": f"Other-{frappe.generate_hash(length=8)}",
				"enabled": 1,
				"api_key": "test_other_key",
			}
		).insert(ignore_permissions=True)
		with patch.object(MollieSettings, "fetch_authoritative_event") as fetch:
			with self.assertRaises(MollieWebhookRejected):
				process_webhook(other.payment_gateway, self.token, "tr_payment1")
		fetch.assert_not_called()


class TestMollieWebhookEndpoint(MollieWebhookTestCase):
	def post(self, *, provider_event=None, enqueue_error=None, token=None, form=None, order=None):
		form_dict = frappe._dict(form or {"id": "tr_payment1"})
		order = order if order is not None else []

		def commit_side_effect():
			order.append("commit")

		def enqueue_side_effect(**kwargs):
			order.append("enqueue")
			if enqueue_error:
				raise enqueue_error

		with (
			patch.object(frappe, "form_dict", form_dict),
			patch.object(
				MollieSettings,
				"fetch_authoritative_event",
				return_value=provider_event or self.authoritative_first(),
			),
			patch.object(frappe.db, "commit", side_effect=commit_side_effect) as commit,
			patch.object(frappe, "enqueue", side_effect=enqueue_side_effect) as enqueue,
		):
			result = mollie_webhook(self.gateway, token or self.token)
		return result, commit, enqueue

	def test_commits_durable_log_before_short_queue_handoff(self):
		order = []
		_, commit, enqueue = self.post(order=order)
		commit.assert_called_once()
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["queue"], "short")
		self.assertEqual(enqueue.call_args.kwargs["doctype"], "Integration Request")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
		self.assertIn(enqueue.call_args.kwargs["docname"], enqueue.call_args.kwargs["job_id"])
		self.assertTrue(frappe.db.exists("Integration Request", enqueue.call_args.kwargs["docname"]))
		self.assertEqual(order, ["commit", "enqueue"])

	def test_queue_outage_propagates_so_mollie_retries(self):
		with self.assertRaisesRegex(RuntimeError, "redis down"):
			self.post(enqueue_error=RuntimeError("redis down"))

	def test_posted_status_amount_and_references_are_ignored(self):
		forged = {
			"id": "tr_payment1",
			"status": "paid",
			"amount": "999999.00",
			"merchant_reference": "forged",
		}
		_, _, enqueue = self.post(provider_event=self.authoritative_first(status="failed"), form=forged)
		log = frappe.get_doc("Integration Request", enqueue.call_args.kwargs["docname"])
		stored = json.loads(log.data)
		self.assertEqual(stored["provider_event"]["payment"]["status"], "failed")
		self.assertEqual(stored["provider_event"]["payment"]["amount"]["value"], "12.50")
		self.assertEqual(
			stored["provider_event"]["payment"]["metadata"]["merchant_reference"],
			"agreement/42",
		)

	def test_forged_route_is_harmless_and_never_queued(self):
		before = frappe.db.count("Integration Request")
		_, commit, enqueue = self.post(token="forged")
		commit.assert_not_called()
		enqueue.assert_not_called()
		self.assertEqual(frappe.db.count("Integration Request"), before)

	def test_paid_payment_without_a_ready_mandate_propagates_for_provider_retry(self):
		with (
			patch.object(frappe, "form_dict", frappe._dict({"id": "tr_payment1"})),
			patch.object(
				MollieSettings,
				"fetch_authoritative_event",
				side_effect=MollieMandatePending("mandate not ready"),
			),
		):
			with self.assertRaisesRegex(MollieMandatePending, "not ready"):
				mollie_webhook(self.gateway, self.token)

	def test_transient_remote_failure_propagates(self):
		with (
			patch.object(frappe, "form_dict", frappe._dict({"id": "tr_payment1"})),
			patch.object(
				MollieSettings,
				"fetch_authoritative_event",
				side_effect=RuntimeError("Mollie unavailable"),
			),
		):
			with self.assertRaisesRegex(RuntimeError, "Mollie unavailable"):
				mollie_webhook(self.gateway, self.token)


class TestMollieWebhookWorker(MollieWebhookTestCase):
	def make_log(self, provider_event=None):
		name, _ = self.process(provider_event or self.authoritative_first())
		return frappe.get_doc("Integration Request", name)

	def test_emits_normalized_first_payment_and_mandate_then_completes(self):
		log = self.make_log()
		with (
			patch("payments.payment_gateways.doctype.mollie_settings.webhook.emit_recurring_event") as emit,
			patch.object(frappe.db, "commit"),
		):
			handle_recurring_notification("Integration Request", log.name)
		self.assertEqual(emit.call_count, 2)
		self.assertEqual(
			[call.args[0]["event_type"] for call in emit.call_args_list],
			["first_payment.updated", "mandate.updated"],
		)
		self.assertEqual(frappe.db.get_value("Integration Request", log.name, "status"), "Completed")

	def test_delivers_fallback_valid_mandate_as_a_separate_resource_event(self):
		provider_event = {
			"payment": payment(
				status="paid",
				mandateId="mdt_pending1",
				paidAt="2026-08-24T12:03:00+00:00",
			),
			"mandate": mandate(id="mdt_valid1", status="valid"),
		}
		# make_log exercises process_webhook and generic normalization before the
		# durable worker consumes the same authoritative bundle.
		log = self.make_log(provider_event)
		with (
			patch("payments.payment_gateways.doctype.mollie_settings.webhook.emit_recurring_event") as emit,
			patch.object(frappe.db, "commit"),
		):
			handle_recurring_notification("Integration Request", log.name)

		payment_event, mandate_event = [call.args[0] for call in emit.call_args_list]
		self.assertEqual(payment_event["payment"]["provider_mandate_id"], "mdt_pending1")
		self.assertNotIn("mandate", payment_event)
		self.assertEqual(mandate_event["mandate"]["provider_mandate_id"], "mdt_valid1")
		self.assertEqual(mandate_event["mandate"]["status"], "valid")
		self.assertEqual(frappe.db.get_value("Integration Request", log.name, "status"), "Completed")

	def test_emits_recurring_payment_and_subscription_events(self):
		provider_payment = payment(
			id="tr_recurring1",
			sequenceType="recurring",
			status="paid",
			mandateId="mdt_mandate1",
			subscriptionId="sub_subscription1",
			paidAt="2026-09-01T12:00:00+00:00",
		)
		log = self.make_log({"payment": provider_payment, "subscription": subscription()})
		with (
			patch("payments.payment_gateways.doctype.mollie_settings.webhook.emit_recurring_event") as emit,
			patch.object(frappe.db, "commit"),
		):
			handle_recurring_notification("Integration Request", log.name)
		self.assertEqual(
			[call.args[0]["event_type"] for call in emit.call_args_list],
			["recurring_payment.updated", "subscription.updated"],
		)

	def test_hook_failure_rolls_back_and_records_failed_request(self):
		log = self.make_log()
		with (
			patch(
				"payments.payment_gateways.doctype.mollie_settings.webhook.emit_recurring_event",
				side_effect=RuntimeError("consumer failed"),
			),
			patch.object(frappe.db, "rollback") as rollback,
			patch.object(frappe.db, "commit") as commit,
		):
			with self.assertRaisesRegex(RuntimeError, "consumer failed"):
				handle_recurring_notification("Integration Request", log.name)
		rollback.assert_called_once()
		self.assertEqual(commit.call_count, 2)
		status, error = frappe.db.get_value("Integration Request", log.name, ["status", "error"])
		self.assertEqual(status, "Failed")
		self.assertIn("consumer failed", error)

	def test_completed_log_is_not_delivered_twice_by_duplicate_jobs(self):
		log = self.make_log()
		frappe.db.set_value("Integration Request", log.name, "status", "Completed")
		with patch("payments.payment_gateways.doctype.mollie_settings.webhook.emit_recurring_event") as emit:
			handle_recurring_notification("Integration Request", log.name)
		emit.assert_not_called()

	def test_exhausted_poison_transition_is_not_delivered_again(self):
		log = self.make_log()
		data = json.loads(log.data)
		data["delivery_attempts"] = _MAX_DELIVERY_ATTEMPTS
		frappe.db.set_value("Integration Request", log.name, {"status": "Failed", "data": json.dumps(data)})
		with patch("payments.payment_gateways.doctype.mollie_settings.webhook.emit_recurring_event") as emit:
			handle_recurring_notification("Integration Request", log.name)
		emit.assert_not_called()


class TestMollieWebhookRecovery(MollieWebhookTestCase):
	def make_log(self, provider_event):
		name, _ = self.process(provider_event)
		return frappe.get_doc("Integration Request", name)

	def test_scheduler_recovers_failed_and_stale_queued_with_deterministic_jobs(self):
		failed = self.make_log(self.authoritative_first(status="failed"))
		stale = self.make_log(self.authoritative_first(status="paid", mandate_status="revoked"))
		frappe.db.set_value("Integration Request", failed.name, "status", "Failed")
		frappe.db.set_value(
			"Integration Request",
			stale.name,
			{"status": "Queued", "modified": "2020-01-01 00:00:00"},
			update_modified=False,
		)

		with patch.object(frappe, "enqueue") as enqueue:
			retry_failed_notifications()

		jobs = {call.kwargs["docname"]: call.kwargs for call in enqueue.call_args_list}
		self.assertIn(failed.name, jobs)
		self.assertIn(stale.name, jobs)
		for name in (failed.name, stale.name):
			self.assertTrue(jobs[name]["deduplicate"])
			self.assertEqual(jobs[name]["job_id"], f"mollie-recurring-delivery:{name}")

	def test_scheduler_does_not_retry_an_exhausted_poison_transition(self):
		poison = self.make_log(self.authoritative_first(status="failed"))
		data = json.loads(poison.data)
		data["delivery_attempts"] = _MAX_DELIVERY_ATTEMPTS
		frappe.db.set_value(
			"Integration Request",
			poison.name,
			{"status": "Failed", "data": json.dumps(data)},
		)
		with patch.object(frappe, "enqueue") as enqueue:
			retry_failed_notifications()
		self.assertNotIn(poison.name, [call.kwargs.get("docname") for call in enqueue.call_args_list])

	def test_exhausted_failed_rows_cannot_starve_stale_queued_recovery(self):
		stale = self.make_log(self.authoritative_first(status="paid", mandate_status="revoked"))
		frappe.db.set_value(
			"Integration Request",
			stale.name,
			{"status": "Queued", "modified": "2020-01-01 00:00:00"},
			update_modified=False,
		)
		poison_data = json.loads(stale.data)
		poison_data["delivery_attempts"] = _MAX_DELIVERY_ATTEMPTS
		for index in range(_RECOVERY_BATCH_SIZE + 1):
			frappe.get_doc(
				{
					"doctype": "Integration Request",
					"name": f"mollie-exhausted-{frappe.generate_hash(length=12)}-{index}",
					"integration_request_service": self.gateway,
					"request_description": "Mollie Recurring Notification",
					"request_id": f"poison-{index}",
					"data": json.dumps(poison_data),
					"is_remote_request": 1,
					"status": "Failed",
				}
			).insert(ignore_permissions=True)

		with patch.object(frappe, "enqueue") as enqueue:
			retry_failed_notifications()

		queued_names = [call.kwargs.get("docname") for call in enqueue.call_args_list]
		self.assertIn(stale.name, queued_names)
		self.assertLessEqual(enqueue.call_count, _RECOVERY_BATCH_SIZE)
