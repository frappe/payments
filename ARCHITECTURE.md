# Architecture

> Adapted from the original architecture notes on `blaggacao/refactor`, from
> which this work descends. This version documents the choices made in **our**
> PaymentController v2: what we kept, what we trimmed, and where we deliberately
> diverged. Differences from the ancestor design are called out inline as
> **[Divergence]**.

The Payments app provides an abstract _PaymentController_ and specific
implementations for a growing number of gateways. These implementations live in
the _Payment Gateways_ module.

Inside the _Payments_ module, a _Payment Gateway_ DocType serves as the link
target from a reference DocType (RefDoc, see below) to the respective gateway
controller and settings. For example, _Payment Request_ links to a _Payment
Gateway_ to implement its payment flow.

On installation the app adds custom fields to the Web Form (for web-form-based
payments) and a _Payment Session Log_ reference on _Payment Request_, and
removes them on uninstallation.

## Relation between RefDoc and Payment Gateway Controller

The reference document implements the surrounding business logic, links to the
_Payment Gateway_, and calls out — typically on submit — to the specific
_PaymentController_ to initiate and handle the transaction.

After a transaction is handled, the RefDoc may post-process the result via one
of the available hook methods and remit a specific payload (e.g. redirect link
or success message) back to the _PaymentController_.

Throughout a payment's lifecycle, state is maintained on the _Payment Session
Log_ (PSL) DocType. It persists free-form data and serves as the central log of
interaction with remote systems.

### TXData, TX Reference and Correlation ID

Data is passed from the RefDoc to the controller via a standardized structure,
_TXData_ (`payments/types.py`). All lifecycle state is then stored on a _Payment
Session Log_.

The **name** of the PSL is the system's unique transaction reference, passed
around between server, client, and remote systems. Gateways should stash it in
request metadata so the remote always returns it, allowing reliable
identification. The payment URL carries it as the `s` query parameter:
`https://my.site.tld/pay?s=<Payment Session Log name>`.

A gateway's _Correlation ID_, when available, is stored as the PSL's
`correlation_id`. If the remote can only echo the correlation id (not the PSL
name), implementations recover the PSL by filtering on
`{"correlation_id": correlation_id}`.

**[Divergence] Security & concurrency hardening.** Our controller adds guards
the ancestor design only gestured at:

- **Tamper whitelist:** at `proceed()`, only `UPDATABLE_TX_DATA_FIELDS`
  (`payer_contact`, `payer_address`, `loyalty_points`, `discount_amount`) may be
  updated from the guest-facing endpoint; critical fields (amount, currency,
  reference doc, `save_mandate`) cannot be changed. Rejected keys are logged.
- **Processing lock + terminal-state guard:** `process_response` takes a
  document lock and re-checks `is_terminal()` after acquiring it, so a webhook
  and a client confirm racing on the same PSL cannot double-process.
- **Fail-fast construction:** `PaymentController.__new__` requires every
  concrete controller to declare `flowstates` and `frontend_defaults`.

## Flow variants

Our controller models these flows:

- **Charge** — a single payment.
- **Mandated Charge** — an off-session payment that requires little or no user
  interaction thanks to a previously stored mandate.
- **Mandate Acquisition** — acquire a mandate for future mandated charges.
  **[Divergence] Reserved but not yet implemented.** It is kept first-class in
  the type model (`SessionType.mandate_acquisition`) so mandate-first gateways
  are not boxed out, but no gateway implements it today.

A _mandate_ represents a pre-authorization to charge a payer off-session
(subscription, SEPA mandate, pre-authorized "hotel booking", tokenized
"one-click"). Concrete mandates subclass `PaymentMandate`
(`payments/controllers/payment_mandate.py`), which declares `is_usable()` and
`revoke()`; gateway-specific identifiers live on the subclass.

### How a mandate is born — two paths

A mandate becomes _usable_ in one of two ways. Only the first is implemented
now:

| Path | When the mandate becomes usable | Gateways | Flow type |
|------|---------------------------------|----------|-----------|
| **First charge saves the mandate** | on first-charge **success** | Stripe (`setup_future_usage=off_session`), Mollie (`sequenceType: first` → mandate `valid` once the first payment succeeds) | `charge` + `TxData.save_mandate` |
| **Direct mandate** | at creation, with **no** charge | GoCardless (mandate-first), Mollie Mandates API (signed SEPA) | `mandate_acquisition` (reserved) |

**[Divergence] Acquisition-as-side-effect, primary path.** The ancestor design
treats mandate acquisition as a first-class, user-interactive step (often a
pre-step of a mandated charge). We instead make the primary acquisition a
**side effect of the first charge**: `save_mandate` signals the charge to
persist/activate a reusable mandate on success. We keep `mandate_acquisition`
reserved for the direct-mandate path rather than implementing it speculatively.

> The transient gateway "pending mandate" (e.g. Mollie creates a `pending`
> mandate the moment a `first` payment starts, valid only after it succeeds) is
> a gateway internal. The framework only persists/links a `PaymentMandate` when
> it becomes usable — i.e. on first-charge success — which is identical for
> Stripe and Mollie.

### Out of scope (deliberate)

Offline SEPA Direct Debit (pain.008 XML generated for manual bank upload, with
pain.002 / return-file reconciliation) is **not** modelled as a gateway here. It
has no API and settles asynchronously by batch — a poor fit for the interactive,
signed-response lifecycle — and the consuming app (Verenigingen) already owns a
mature offline-SDD subsystem. The controller earns its keep only for
PSP-mediated flows where there is a real API and a response to process.

## RefDoc Flow

1. Call the controller's `initiate` staticmethod with `tx_data` and optionally a
   pre-selected _Payment Gateway_; store the returned PSL name for reference.
2. If a gateway was pre-selected, call the returned controller's
   `is_user_flow_initiation_delegated(psl_name)`. If `True`, the controller
   takes over the user flow; if `False`, the RefDoc business logic drives the
   next steps.
3. If not delegated: initiate/continue the user flow (email, SMS, link, etc.).
4. Post-process status changes via the optional RefDoc hook
   `on_payment_<flow>_processed(changed, state, flags, flowstates)` — where
   `<flow>` is `charge` or `mandated_charge` — with two goals:
   - continue business logic in the backend;
   - optionally return `{"message": _("..."), "action": {"href": "...",
     "label": _("...")}}` to the controller (`message` shown to the user;
     `action` rendered as the call-to-action). If nothing is returned, the
     gateway's or app's default is used.
   On decline, `on_payment_failed(message)` is also invoked if present.

> **[Divergence] Hook signature.** The ancestor sketch used
> `on_payment_*_processed(flags, state)`; ours passes
> `(changed, state, flags, flowstates)` so hooks can see whether the status
> actually changed and the controller's full state.

## PaymentController Flow

We delay remote interaction as late as possible — to initialize timeouts late
and keep customer choices open until the last moment.

1. `initiate` throws if `validate_tx_data` rejects the data; otherwise creates
   the PSL (status **Created**).
2. **Interactive charge** — wait for the user GO signal (link, click, SMS), then
   `proceed(psl_name, updated_tx_data)`. The controller may apply whitelisted
   `tx_data` updates from user input. `proceed` runs the shared initiation core
   for the `charge` flow (status **Initiated**).
3. **Headless mandated charge** — **[Divergence]** a merchant-initiated renewal
   has *no* user GO signal, so it cannot flow through `proceed()`. Instead the
   backend calls the trusted (non-whitelisted) `charge_mandate(mandate,
   tx_data, gateway)`, which runs the same initiation core for the
   `mandated_charge` flow. For gateways that confirm synchronously
   (Stripe `off_session=True, confirm=True`) the result is available
   immediately and is fed straight into `process_response`.
4. **Shared initiation core.** Both entry points call
   `_run_initiation(psl, flow_type)`, which dispatches to the flow's
   `_initiate_*` method, persists `correlation_id` + initiation payload +
   `flow_type`, and returns the `Initiated` result. There is exactly **one**
   initiate path; the two entry points differ only in how they present errors
   (redirect vs returned `Processed`).
5. The actual capture proceeds in collaboration with the gateway (client flow or
   server-to-server).
6. `process_response(psl_name, payload)` recovers the controller and dispatches
   on `flow_type` (`_FLOW_DISPATCH`):
   - `_validate_response()` checks payload integrity (e.g. signature against a
     shared key);
   - `_process_response_for_<flow>()` maps the gateway status onto
     `flowstates` (`success` / `pre_authorized` / `processing` / `declined`);
   - the optional RefDoc hook runs and may override the user-facing result;
   - the new status is persisted (**Paid** / **Authorized** / **Processing** /
     **Declined** / **Cancelled** / **Error** / **Error - RefDoc**).

**[Divergence] `requires_action` on a headless charge.** If an off-session
mandated charge comes back needing customer action (SCA, or the mandate needs
re-authorization), `charge_mandate` returns a `Processed` whose action points at
the `/pay?s=<PSL>` URL. The caller (e.g. a dues job) can email that link; the
*same* PSL then completes through the normal interactive path. `flowstates` is
left untouched — the branch lives in `charge_mandate`, not in a global
reclassification.

### Idempotency

`process_response` is expected to be idempotent: a server-to-server gateway
response and a client-flow signed payload may arrive in parallel. The processing
lock only ensures that parallel processing does not race; the terminal-state
re-check makes a second arrival a no-op that returns the already-final status.

### Sequence Diagram

```mermaid
sequenceDiagram
    participant RefDoc
    participant Backend as Backend (renewal job)
    participant PaymentController
    actor Payer
    actor Gateway
    autonumber

    rect rgb(200, 150, 255)
    Note over RefDoc, Gateway: Interactive charge (first payment; may save a mandate)
    RefDoc->>+PaymentController: initiate(txdata, payment_gateway)
    Note over PaymentController: Status - "Created"
    Payer ->> PaymentController: proceed(pslname, updated_txdata)
    Note over PaymentController: _run_initiation(charge) -> "Initiated"
    PaymentController->>+Gateway: _initiate_charge()
    alt IPN (server-to-server)
        Gateway->>-PaymentController: process_response(pslname, payload)
    else Client flow
        Gateway-->>Payer: redirect / widget
        Payer->>PaymentController: process_response(pslname, payload)
    end
    end

    rect rgb(255, 210, 130)
    Note over Backend, Gateway: Headless mandated charge (renewal; no user GO signal)
    Backend->>+PaymentController: charge_mandate(mandate, txdata, gateway)
    Note over PaymentController: _run_initiation(mandated_charge)
    PaymentController->>Gateway: _initiate_mandated_charge() [off_session, confirm]
    Gateway-->>PaymentController: synchronous result
    alt requires_action
        PaymentController-->>Backend: Processed(action -> /pay?s=PSL)
        Note over Backend, Payer: email re-auth link; same PSL resumes interactive path
    else resolved
        PaymentController->>PaymentController: process_response(pslname, payload)
    end
    end

    rect rgb(70, 200, 255)
    PaymentController -->> PaymentController: _validate_response()
    PaymentController -->> PaymentController: _process_response_for_*()
    opt RefDoc implements hook
    PaymentController ->> RefDoc: on_payment_*_processed(changed, state, flags, flowstates)
    RefDoc-->>PaymentController: return_value
    end
    PaymentController -->> PaymentController: persist status (Paid|Authorized|Processing|Declined|Cancelled|Error|Error - RefDoc)
    end
```

> **Notes:**
>
> - A server-to-server gateway response and a signed client-flow payload may
>   occur in parallel; the blue area must therefore be **idempotent**.
> - The processing lock only guarantees that parallel processing does not race.

### The Payment URL

A well-known, short, gateway-agnostic URL with an `s` query parameter. The page
at that URL captures the user's GO signal for the controller flow and renders
terminal results. Kept tidy to convey trustworthiness:
`https://my.site.tld/pay?s=<Payment Session Log name>`.

## Other Folders

- `payments/utils` — general utilities in `utils.py`, re-exported via
  `__init__.py` for convenient namespacing.
- `payments/overrides` — overrides of standard Frappe code (currently the
  WebForm controller and a WebForm whitelisted method).
- `payments/templates` — gateway-specific custom checkout pages.
- `payments/types.py` — types and dataclasses for IDE-assisted integration
  development.
- `payments/exceptions.py` — the app's exceptions.
- `payments/controllers/payment_controller.py` — the controller base class;
  `payments/controllers/payment_mandate.py` — the mandate base class.
- `payments/www/pay.{py,js,css,html}` — the unified checkout page.
- `payments/payment_gateways/doctype/payment_demo_settings/` — a dependency-free
  reference/demo controller that doubles as the framework's test vehicle.

## Relationship to the ancestor design (blaggacao/refactor)

We took blaggacao's design, **trimmed** the speculative mandate surface (the
full three-variant scaffolding was removed as unimplemented), **hardened** the
charge path (tamper whitelist, locking, fail-fast construction), and re-added a
**narrow, consumer-backed** slice of the mandate model. The key conceptual
divergence: the ancestor funnels every variant through the user-present
`proceed()`, which cannot express a charge with no user GO signal; we add a
headless `charge_mandate` entry point over a shared initiation core. Mandate
acquisition stays first-class in the type model but unimplemented until a
mandate-first gateway needs it.
