# Provider-neutral recurring payments contract

`payments.recurring` defines contract version `1` between an application that owns a recurring agreement and a payment-gateway settings controller. It does not own a subscription record and contains no provider-specific, webhook, scheduler, persistence, LMS, Commerce, or reference-DocType behavior.

The controller is always resolved with:

```python
payments.utils.utils.get_payment_gateway_controller(payment_gateway)
```

This preserves the existing `Payment Gateway` settings/controller indirection, including named controllers. Callers and providers exchange only bounded JSON-serializable mappings. Provider identifiers are opaque, trimmed strings: the core never parses their prefixes or formats.

## Public API

```python
from payments import recurring

webhook_url = recurring.get_recurring_webhook_url(payment_gateway)
payment = recurring.begin_first_payment(payment_gateway, request)
subscription = recurring.activate_subscription(payment_gateway, request)
subscription = recurring.cancel_subscription(payment_gateway, request)
payment = recurring.retry_payment(payment_gateway, request)
subscription = recurring.reconcile_subscription(payment_gateway, request)
events = recurring.normalize_provider_events(payment_gateway, authoritative_provider_event)
recurring.emit_recurring_event(event)
```

Every provider operation resolves the gateway controller through the existing indirection and invokes a controller method with the same name. `get_recurring_webhook_url` calls its controller capability without arguments; the mapping-based operations pass one normalized immutable request. A missing or non-callable method raises `RecurringPaymentCapabilityError`. In particular, `retry_payment` never falls back to creating another charge.

All request, result, snapshot, and event mappings reject unknown fields. `contract_version` is optional on input, defaults to `1`, is always present in normalized output, and rejects any value other than integer `1`.

Normalized mappings and nested metadata are immutable `dict`/`list` subclasses that remain JSON serializable. `normalize_provider_events` returns a list whose event items are immutable normalized mappings. Normalization makes independent copies and never retains mutable caller or provider aliases.

## Shared scalar rules

- `merchant_reference`, `customer_reference`, `idempotency_key`, event IDs, and provider IDs are required non-empty, trimmed opaque strings of at most 255 characters unless marked optional.
- Control characters are forbidden in identifiers and text.
- `customer_name` is trimmed non-empty text of at most 255 characters. `customer_email` is a plain, trimmed address of at most 320 characters with exactly one `@`, a non-empty local part of at most 64 characters, a non-empty domain of at most 253 characters, and no whitespace or control characters. Provider-specific deliverability remains the provider's responsibility.
- `customer_locale` uses the portable `ll_CC` form (for example, `en_GB`): two lowercase language letters, an underscore, and two uppercase country letters. Providers may reject syntactically valid locales they do not support.
- `amount` must be a positive finite `Decimal` or canonical fixed-point decimal string (for example, `"12.50"`). Floats, integers, exponent notation, signs, and leading-zero forms are rejected for string inputs. Normalized amounts are fixed-point strings, with at most 18 integer and 9 fractional digits.
- `currency` must contain exactly three ASCII letters and is normalized to uppercase.
- `interval` is a portable single-unit ISO-8601 duration: `P<n>D`, `P<n>W`, `P<n>M`, or `P<n>Y`, where `n` is positive.
- Dates use exact `YYYY-MM-DD`. Timestamps are RFC-3339/ISO-8601 strings with an explicit timezone.
- URLs must be absolute HTTPS URLs, contain no credentials, fragments, control characters, or backslashes, and be at most 2,048 characters. Redirect and checkout URLs may use HTTP only for `localhost`, `127.0.0.1`, or `::1`; webhook URLs always require HTTPS.
- Optional `metadata` is JSON data limited to 32 top-level keys, 32 keys/items per nested container, four nesting levels, 64-character string keys, and 4 KiB serialized. Non-finite numbers and non-JSON values are rejected.

## Recurring webhook transport URL

`get_recurring_webhook_url(payment_gateway)` resolves the selected Payment Gateway controller and calls its no-argument `get_recurring_webhook_url` method. The controller owns account-safe provider routing; the consumer does not construct provider routes or import provider code. The result must be one absolute HTTPS URL with no credentials, fragment, control characters, or backslashes. HTTP is rejected even for local hosts. The URL is suitable for the `webhook_url` request field; transport authentication and authoritative provider fetches remain the adapter's responsibility.

## Requests

### `begin_first_payment`

Required fields:

```text
merchant_reference, customer_reference, amount, currency, description,
redirect_url, webhook_url, idempotency_key
```

Optional fields: `contract_version`, `metadata`, `provider_customer_id`, `customer_name`, `customer_email`, `customer_locale`.

For a new provider customer, omit `provider_customer_id` and supply both `customer_name` and `customer_email`; the controller creates the provider customer and returns its opaque ID in the payment snapshot. For reuse, supply `provider_customer_id`; name and email may then be omitted. If optional name, email, or locale values are supplied in either flow, they are normalized and bounded as documented above. A provider result cannot change a supplied `provider_customer_id`.

The provider begins the on-session first payment used to establish a mandate. The result is a payment snapshot.

### `activate_subscription`

Required fields:

```text
merchant_reference, customer_reference,
provider_customer_id, provider_mandate_id, mandate_status,
amount, currency, interval, start_date, description, webhook_url,
idempotency_key
```

Optional fields: `contract_version`, `metadata`, `payment_count`.

`payment_count`, when present, is a positive integer and defines a finite schedule. Its omission explicitly defines an indefinite schedule. Providers MUST return it in the subscription snapshot when it was supplied.

`mandate_status` must be exactly `valid`. The caller or provider transport must obtain that status from an authoritative provider response. The core does not discover a mandate and does not permit activation from a pending, invalid, revoked, or expired mandate. The result is a subscription snapshot.

### `cancel_subscription`

Required fields:

```text
merchant_reference, customer_reference, provider_customer_id,
provider_subscription_id, idempotency_key
```

Optional fields: `contract_version`, `metadata`. `provider_customer_id` supplies the account scope required by customer-scoped provider APIs. The result is an authoritative normalized subscription snapshot.

### `retry_payment`

Required fields:

```text
merchant_reference, customer_reference, provider_payment_id,
idempotency_key
```

Optional fields: `contract_version`, `metadata`. The result is a payment snapshot.

A controller must implement this operation only when the provider has a safe, idempotent retry primitive for the known payment. Unsupported providers should omit the method or raise `RecurringPaymentCapabilityError`; they must not create a replacement charge under this operation.

### `reconcile_subscription`

Required fields:

```text
merchant_reference, customer_reference, provider_customer_id,
provider_subscription_id
```

Optional fields: `contract_version`, `metadata`.

Reconciliation is read-only, so it deliberately has no idempotency key. The owning application schedules recovery and supplies its stored opaque customer and subscription provider IDs. The result is the same subscription snapshot shape returned by activation and cancellation.

## Result snapshots

### Payment snapshot

Required fields:

```text
provider_payment_id, status, amount, currency,
merchant_reference, customer_reference
```

Optional fields:

```text
contract_version, checkout_url, provider_customer_id,
provider_mandate_id, provider_subscription_id, refunded_amount,
charged_back_amount, occurred_at, metadata
```

Statuses: `open`, `pending`, `authorized`, `paid`, `failed`, `canceled`, `expired`, `partially_refunded`, `refunded`, `partially_charged_back`, `charged_back`. `authorized` preserves providers that distinguish authorization from capture.

`refunded_amount` and `charged_back_amount` are positive decimal amounts in the snapshot currency. Each must not exceed the original `amount`, and together they must not exceed it. Reversal normalization is deterministic: any chargeback takes precedence over refund status selection; a chargeback equal to the original amount uses `charged_back`, otherwise `partially_charged_back`. Without a chargeback, a refund equal to the original amount uses `refunded`, otherwise `partially_refunded`. A reversal status requires its corresponding amount field, and a supplied reversal amount requires the matching normalized status. Both amount fields remain present when refunds and chargebacks coexist, so consumers do not lose monetary state.

### Subscription snapshot

Required fields:

```text
provider_subscription_id, status,
merchant_reference, customer_reference,
provider_customer_id, provider_mandate_id,
amount, currency, interval, start_date, description
```

Optional fields: `contract_version`, `payment_count`, `next_payment_at`, `canceled_at`, `occurred_at`, `metadata`.

`payment_count` is required in the snapshot for finite schedules and omitted for indefinite schedules. Statuses: `pending`, `pending_mandate`, `active`, `suspended`, `canceled`, `completed`, `failed`. `pending` preserves a provider's authoritative pending subscription state; `pending_mandate` remains available for providers that expose that more specific portable state.

### Mandate snapshot

Required fields:

```text
provider_mandate_id, status,
merchant_reference, customer_reference, provider_customer_id
```

Optional fields: `contract_version`, `occurred_at`, `metadata`.

Statuses: `pending`, `valid`, `invalid`, `revoked`, `expired`.

Provider results are checked against request correlation fields. A controller cannot silently change a merchant/customer reference, amount, currency, interval, start date, description, finite payment count, or supplied provider ID.

## Provider event normalization

`normalize_provider_events(payment_gateway, provider_event)` passes one bounded immutable copy to the controller's `normalize_provider_events` method. The transport-specific input has no field allowlist because each provider has a different authoritative resource shape. It is limited to JSON data, 64 KiB, 12 nesting levels, and 256 keys/items per container.

This function does **not** authenticate a webhook. A provider transport must authenticate the request or fetch the referenced resource from the provider API and pass authoritative data. Posted status, amount, ownership, or references must not be trusted merely because they arrived at a webhook endpoint.

The controller returns a list of exact recurring-event mappings. The core injects `payment_gateway`; a provider cannot claim a different gateway.

Required event fields:

```text
event_id, event_type, merchant_reference, customer_reference
```

Optional fields:

```text
contract_version, payment_gateway, provider_created_at,
payment, mandate, subscription, metadata
```

Event types and required snapshots:

| Event type | Required snapshot |
| --- | --- |
| `first_payment.updated` | `payment` |
| `mandate.updated` | `mandate` |
| `subscription.updated` | `subscription` |
| `recurring_payment.updated` | `payment` |

All snapshots in an event must carry the same merchant and customer references as the event. When extra snapshots share `provider_customer_id`, `provider_mandate_id`, or `provider_subscription_id`, those opaque identifiers must also match.

## Event delivery and idempotency

`emit_recurring_event(event)` validates the exact event shape and calls one generic hook:

```python
call_hook_method("recurring_payment_event", event=normalized_event)
```

The core delivery helper is synchronous only: it does not itself provide at-least-once delivery, persistence, deduplication, or retry. To provide at-least-once delivery, a provider transport **MUST** durably persist and deduplicate each authoritative transition before calling the hook and **MUST** retry when delivery raises.

`event_id` **MUST** be stable for every redelivery of one authoritative resource-state transition and **MUST** differ for later authoritative status transitions of that resource. For payment events, transition identity **MUST** include the normalized status and the normalized `refunded_amount` and `charged_back_amount` values when present, so two partial reversals of the same payment remain distinguishable even when the status string is unchanged. A mutable provider resource ID alone is therefore insufficient when the same resource can change status; adapters can derive an opaque transition ID from the account-scoped resource ID plus authoritative status, reversal amounts, version, or timestamp data. Consumers must make writes idempotent using `payment_gateway + event_id` (and their own agreement identity as appropriate), tolerate duplicate and out-of-order updates, and never assume exactly-once hook execution.

The core deliberately does not call a method on a `reference_doctype`, commit a transaction, enqueue a job, persist an agreement, or decide application access policy.

## Provider implementation checklist

1. Implement only supported methods on the settings controller resolved by `Payment Gateway`; when webhooks are supported, return an account-safe HTTPS transport endpoint from the no-argument `get_recurring_webhook_url` capability.
2. Make every mutating operation idempotent under the supplied `idempotency_key`.
3. Return only the documented normalized fields; keep raw provider payloads and secrets out of results and metadata.
4. Obtain webhook state authoritatively and account-scope it before normalization.
5. Preserve opaque merchant/customer references in provider metadata so events can correlate without importing the consumer app.
6. Return unsupported retry capability rather than creating a duplicate charge.
7. Durably persist and deduplicate authoritative transitions, use transition-stable event IDs, and retry generic hook delivery to provide at-least-once delivery.

## Version-16 backport constraints

The develop branch currently targets Frappe 17 and a newer Python runtime. A production v16 release needs a dedicated branch from Payments `version-16`, not an install of develop. Keep the public schema and behavior identical while adapting only branch-specific typing, test base classes, and packaging metadata. Validate the backport in a Frappe v16 Bench; compile/static checks alone cannot attest DocType controller resolution or app-hook integration.
