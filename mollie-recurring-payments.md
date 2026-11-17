# Mollie recurring payments

Payments implements Mollie as a provider for the provider-neutral contract in [`payments.recurring`](recurring-payments.md). The adapter owns Mollie API calls and webhook transport only. It does not own a business subscription, purchase, entitlement, payment-retry policy, agreement registry, or consumer DocType. Its provider-specific recovery task retries only durable webhook transport rows.

## Configure an account

Create one **Mollie Settings** document for each Mollie account/profile routing context:

- **Payment Gateway Name** is the local account name.
- **API Key** is the Mollie `test_…` or `live_…` key and is stored as an encrypted Password field.
- **Enabled** controls recurring operations.

Saving creates the named Payment Gateway `Mollie-<Payment Gateway Name>` through the standard `Payment Gateway -> Mollie Settings` controller indirection. A cryptographically random encrypted routing token is generated on insert. System Manager is the only role with Settings permissions.

Consumers obtain the account-scoped callback with the generic API rather than constructing a Mollie route:

```python
from payments.recurring import get_recurring_webhook_url

webhook_url = get_recurring_webhook_url("Mollie-Primary")
```

The site URL must be HTTPS. The routing token selects exactly one Settings controller; it is not treated as proof of payment state.

## First payment and mandate

`begin_first_payment`:

1. Reuses `provider_customer_id` only after fetching it from the selected Mollie account and matching its contract version and opaque `customer_reference`; otherwise creates a customer from the normalized name/email/locale.
2. Creates a customer-scoped Mollie payment with `sequenceType = first`.
3. Sends only contract version and opaque merchant/customer references as Mollie metadata.
4. Uses deterministic, operation-specific `Idempotency-Key` headers derived from the account and caller idempotency key.
5. Requires `webhook_url` to exactly match the selected controller's generic `get_recurring_webhook_url` result, preventing cross-account callback routing.
6. Returns a normalized payment snapshot and Mollie checkout URL.

The Mollie webhook posts a payment `id` only. Payments fetches that payment with the routed account API key. A paid first payment causes an authoritative mandate-list fetch; its named mandate is preferred only when valid, otherwise another valid mandate is selected. If none is authoritative yet, the request fails retryably instead of permanently acknowledging the transition. The adapter emits `first_payment.updated` and `mandate.updated`. A consumer must wait for authoritative `mandate.status == valid` before activation.

## Subscriptions

`activate_subscription` requires an already valid mandate, enforces the selected account's webhook URL, and creates `/customers/{customer}/subscriptions`. Portable intervals (`P1D`, `P2W`, `P1M`, `P1Y`) are translated to/from Mollie intervals. `start_date`, description, amount/currency, mandate, and optional finite `payment_count` round-trip through the normalized snapshot.

`cancel_subscription` first fetches and correlates the account-scoped subscription. An arbitrary unknown ID therefore fails. A bounded 30-day Redis receipt is keyed by the Mollie account plus the complete caller/idempotency/resource correlation only after that authoritative GET; it makes an already-canceled subscription and the known post-delete 404 path repeatably idempotent without committing the consumer's database transaction. A missing/expired receipt fails closed for an absent resource rather than turning an arbitrary ID into success. `reconcile_subscription` performs an authoritative customer-scoped GET.

Mollie controls subscription retries. `retry_payment` explicitly raises `RecurringPaymentCapabilityError`; it never creates a duplicate replacement charge.

## Webhook durability and security

Endpoint:

```text
/api/method/payments.payment_gateways.doctype.mollie_settings.webhook.mollie_webhook
```

Mollie webhooks are not signed. The implementation therefore:

- accepts only a strict `tr_…` payment ID as an untrusted hint;
- constant-time checks the account-routing token;
- fetches the payment with the selected account API key before persisting anything;
- ignores every posted status, amount, currency, reference, customer, mandate, or subscription value;
- fetches a mandate for paid first payments and the customer-scoped subscription for recurring payments;
- projects the authoritative response to the normalized fields and opaque contract metadata needed by the worker, so callback tokens, billing details, and unrelated provider data are not persisted;
- persists that sanitized authoritative bundle in a deterministic Integration Request keyed by account plus normalized resource-state transition, not payment ID alone;
- commits the log before enqueueing a short worker;
- reuses queued/failed transitions, preserves attempt counts across provider redelivery, and does not redeliver completed transitions;
- commits each worker-attempt boundary, persists hook failures as Failed, then re-raises the worker error;
- runs a provider-specific ten-minute recovery task for Failed and stale Queued rows, using deterministic deduplicated job IDs and a maximum of 12 delivery attempts per transition;
- lets transient Mollie API, eventually-consistent mandate, and queue failures propagate so Mollie retries;
- treats malformed routes and account-scoped 404s as harmless, non-retryable hints;
- normalizes through `payments.recurring` and emits only `recurring_payment_event` hooks.

Each event ID is resource-local: a payment event changes only for that payment's status or normalized reversal amount, while mandate and subscription IDs change only for their own status. The aggregate Integration Request fingerprint includes every event ID, so a sibling transition remains durably distinct without pretending the unchanged payment transitioned again.

Mollie's `amountRefunded` and `amountChargedBack` are normalized as partial/full refund or chargeback statuses with their respective amount fields. Because the payment resource supplies no trustworthy reversal timestamp, reversal events omit `occurred_at`; canceled subscriptions use `canceledAt`, not `createdAt`.

Hook delivery is retried at least once up to the explicit 12-attempt poison-event bound. Exhausted rows remain Failed for operational inspection and explicit administrative intervention. Consumers must deduplicate by Payment Gateway and event ID and tolerate out-of-order transitions.

## No hidden persistence policy

Integration Request is transport/audit persistence, not a subscription registry. The owning application stores opaque Mollie IDs, schedules reconciliation, decides retries/cancellation policy, and updates its own subscription/entitlement records from normalized idempotent events.

## Validation

Run in a Bench containing Payments:

```bash
bench --site <site> run-tests --app payments \
  --module payments.payment_gateways.doctype.mollie_settings.test_mollie_settings
bench --site <site> run-tests --app payments \
  --module payments.payment_gateways.doctype.mollie_settings.test_webhook
```

A dedicated Payments `version-16` backport and Frappe v16 Bench run are required before production use.
