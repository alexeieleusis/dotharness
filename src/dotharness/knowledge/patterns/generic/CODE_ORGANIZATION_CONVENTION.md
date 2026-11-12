# Code Organization Convention (Generic)

**The comprehensive, framework-agnostic reference. Read the quick guides first. Come here for the "why".**

---

## Goals

- **Testability** - business rules verifiable without a database, server, browser, or framework.
- **Changeability** - swapping a framework, transport, or datastore touches adapters, not rules.
- **Readability** - a newcomer can predict where any piece of logic lives.
- **Small blast radius** - a change to one concern does not ripple through unrelated files.
- **Portability of knowledge** - the same mental model works across tiers and stacks.

## Non-Goals

- Prescribing folder names, file suffixes, or class-vs-function style.
- Maximizing layers. Every layer must earn its place (see [When to Skip a Role](#when-to-skip-a-role)).
- Replacing a codebase's existing conventions.

---

## Table of Contents

1. [The Roles](#the-roles)
2. [Dependency Flow](#dependency-flow)
3. [Contracts (Types)](#contracts-types)
4. [Domain Logic](#domain-logic)
5. [Adapters](#adapters)
6. [State](#state)
7. [Actions](#actions)
8. [Orchestrator](#orchestrator)
9. [Entry Point](#entry-point)
10. [Presentation / Serialization](#presentation--serialization)
11. [Testing Strategy](#testing-strategy)
12. [Optional Exploratory Artifacts](#optional-exploratory-artifacts)
13. [Best Practices](#best-practices)
14. [When to Skip a Role](#when-to-skip-a-role)
15. [Examples Across Stacks](#examples-across-stacks)
16. [Summary](#summary)

---

## The Roles

A feature is decomposed by **responsibility**, not by technology. Eight roles cover almost everything:

| Role                          | Responsibility                                              | Pure? | Does I/O? |
| ----------------------------- | ----------------------------------------------------------- | ----- | --------- |
| **Contracts**                 | Types/schemas for inputs, outputs, and internal data shapes | n/a   | No        |
| **Domain**                    | Business rules: validation, transformation, decisions       | Yes   | No        |
| **Adapters**                  | Talk to the outside world (DB, HTTP, FS, clock, UI toolkit) | No    | Yes       |
| **State**                     | Mutable or derived state that lives for the feature's span  | No    | No        |
| **Actions**                   | Use-cases: sequence domain calls and adapter calls          | No    | Via adapters |
| **Orchestrator**              | Compose the above and expose one cohesive surface           | No    | No        |
| **Entry point**               | Receive an external trigger; delegate to the orchestrator   | No    | Edge only |
| **Presentation/Serialization**| Turn results into the output format (view, response, report)| Yes   | No        |

Not every feature needs every role. The roles are a **checklist of concerns**, not a mandatory file list.

### Role Aliases by Stack (illustrative)

| Generic role   | Frontend component | HTTP service      | CLI tool         | Batch/worker job   |
| -------------- | ------------------ | ----------------- | ---------------- | ------------------ |
| Contracts      | prop/state types   | DTOs/schemas      | arg/option types | message schemas    |
| Domain         | pure helpers       | domain services/policies | pure helpers | pure helpers  |
| Adapters       | API client hooks   | repositories, gateways | fs/process wrappers | queue/store clients |
| State          | local UI state     | request context / unit of work | run options | job progress     |
| Actions        | event handlers     | use-case / service methods | command bodies | step handlers    |
| Orchestrator   | main hook/composable | module/service facade | command setup  | job runner         |
| Entry point    | container component| controller/handler | `main`/command  | consumer/handler   |
| Presentation   | presentational view| serializer/response mapper | formatter  | report/result writer |

---

## Dependency Flow

Dependencies point **inward, toward the domain**. Each step depends only on the steps before it.

```
Contracts (shared by all)
  ↓
Domain (pure; depends on nothing but contracts)
  ↓
Adapters (implement I/O; may use domain types)
  ↓
State (depends on domain for derived values)
  ↓
Actions (depend on domain + adapters + state)
  ↓
Orchestrator (wires actions, state, adapters)
  ↓
Entry point (calls orchestrator)
  ↓
Presentation/Serialization (renders what the orchestrator returns)
```

### Rules

- **Domain imports nothing** from adapters, state, actions, or the framework.
- **No cycles.** If A needs B and B needs A, extract the shared piece downward.
- **Adapters are injected**, not imported statically by domain or actions where it would block testing. Pass them in (constructor, parameter, context) or isolate behind an interface you can fake.
- **Entry points never contain rules.** If you find an `if` that encodes a business decision, it belongs in the domain.
- **Skipping a layer is fine; reversing one is not.**

---

## Contracts (Types)

Define the shapes first. They are the cheapest, most reviewable artifact and drive everything else.

- **Input contract** - what the entry point accepts.
- **Output contract** - what the feature returns/emits.
- **Internal data shapes** - entities, value objects, intermediate results.
- **Dependency contracts** - the interfaces adapters must satisfy.

### Progressive Props

When composing, give each step the **original input plus the results of earlier steps**, never reaching "sideways" or "backwards":

```
StepAInput = Input
StepBInput = Input + StepAResult
StepCInput = Input + StepAResult + StepBResult
```

This makes the linear dependency flow visible in the types.

### Public vs. Internal Surfaces

Sometimes a part exposes mutators to collaborators, and outsiders must not call them. In that case, define two shapes: an **internal** one (with mutators) and a **public** one (read-only). Strip the internal surface before the orchestrator returns.

### In Dynamically Typed Languages

Use schemas, documented dict shapes, dataclasses, or doc-comments. The discipline - *name and centralize the shapes* - matters more than the type checker.

---

## Domain Logic

The heart of the pattern. **Everything that encodes a business decision lives here.**

### What Belongs

- Validation rules
- Transformations between representations (form → request, row → entity, event → command)
- Business predicates ("can this be deleted?", "is this eligible?")
- Calculations and derivations
- Policy decisions (pricing, retry eligibility, routing)

### What Does Not

- Reading or writing anything (DB, network, files, clock, randomness, env)
- Logging, metrics, notifications
- Framework types (request objects, ORM models, UI components)
- Mutation of inputs

### Shape

Prefer functions of the form `(inputs) → result`, where the result is **data describing the outcome**, not an effect:

```
validate(input)      → { valid: bool, errors: [...] }
toRequest(input)     → Request
canDelete(e, links)  → { allowed: bool, reason?: string }
```

Return *reasons*, not just booleans, so callers can explain decisions without re-deriving them.

### Time, Randomness, and IDs

These are I/O. Pass them in as arguments (`now`, `rng`, `newId`) so the function stays pure and deterministic.

### Key Principles

- **Deterministic**: same input → same output.
- **Total where possible**: return an error value rather than throwing for expected failures.
- **Small and named**: one rule per function, named for the rule.
- **Framework-free**: you should be able to run these in a bare REPL.

---

## Adapters

Adapters isolate the world outside your process.

- One adapter per **external concern** (a datastore, a remote API, the filesystem, a message bus, the UI toolkit).
- Adapters **translate** between external representations and your contracts. External shapes must not leak inward.
- Adapters **hide their handles**: expose simple operations (`create(data)`, `find(id)`), not raw connection, transaction, or mutation objects.
- Error handling at the adapter boundary: map external failures into a small, domain-meaningful set of error kinds.
- Retries, timeouts, caching and idempotency keys belong here, configured not hard-coded.

### Consolidation

Adapters for a shared resource (e.g. "all user queries") should live in one consolidated place, not be re-declared per feature. Features **consume** adapters; they rarely define new ones for a resource that already has one.

---

## State

State is anything that changes across the feature's lifetime: UI flags, a request-scoped context, a job's progress, an in-memory cache.

- Keep it **minimal** - derive what you can instead of storing it.
- **Derived values come from domain functions**, not inline expressions.
- Separate **public state** (read by consumers) from **internal state** with mutators (used by actions). See [Public vs. Internal Surfaces](#public-vs-internal-surfaces).
- Stateless features (a pure request/response handler) skip this role entirely.

---

## Actions

Actions are the feature's **use-cases**: the verbs.

An action:

1. Gathers inputs (from state, parameters, context).
2. Calls **domain functions** to validate/decide/transform.
3. Calls **adapters** to perform effects.
4. Updates state and/or reports results.

An action **does not** contain business rules. It *curries* domain functions with the current context. Test: read the action top to bottom. If it reads as "call, call, call", it is correct. If it has branching that encodes a business decision, extract that branch to the domain.

### Canonical Shape

```
action(context):
    result = domain.validate(context.input)
    if not result.valid:
        return report(result.errors)
    request = domain.toRequest(context.input)
    outcome = adapters.save(request)
    state.markSaved(outcome)
    return outcome
```

### Error Handling

Actions own **when** to surface errors (and in what channel: toast, HTTP status, exit code, log). Domain owns **what** the errors are. Adapters own **how** external failures map to error kinds.

---

## Orchestrator

The orchestrator composes the parts in dependency order and exposes one surface.

- **Boring on purpose**: construct/obtain each part, pass results forward, return the combined surface.
- **No logic**: no conditionals that encode behavior, no inline handlers, no state of its own.
- **Strips internals** before returning (see Public vs. Internal Surfaces).
- This is your **composition root** for the feature - the one place that knows how everything fits.

Heuristic: if a reviewer must *think* to understand the orchestrator, something belongs elsewhere.

---

## Entry Point

The entry point is where the outside world triggers the feature: a route handler, a UI component, a CLI command, a queue consumer, a scheduled job.

- **Translate in**: parse/authenticate/coerce the external input into the input contract.
- **Delegate**: call the orchestrator.
- **Translate out**: hand the result to presentation/serialization.
- **Nothing else.** No rules, no direct adapter calls, no persistent state.

A thin entry point makes the feature reusable: the same orchestrator can serve a web route *and* a CLI command. It also keeps framework churn cheap.

---

## Presentation / Serialization

Turns the feature's result into its final form: a rendered view, an HTTP response body, a printed report, an emitted event.

- **Pure function of its inputs** where the stack allows it.
- Receives exactly what the orchestrator returns - no extra fetching.
- Splitting presentation from the entry point lets you render it with fake data in isolation (snapshot tests, a component catalog, golden-file tests).

---

## Testing Strategy

### Priority

1. **Domain tests (highest)** - pure, fast, exhaustive over the rules. This is where most bugs are cheapest to catch.
2. **Unit tests for actions/state/adapters** - with collaborators faked.
3. **Integration tests** - the orchestrator with real internal parts, fake external edges.
4. **Contract tests** - adapters against the real (or recorded) external system, verifying your assumptions about it.
5. **End-to-end / system tests** - few, covering critical journeys only.
6. **Presentation tests** - snapshot/golden/visual tests, only where output format is itself a requirement.

### Principles

- **Test behavior, not wiring.** Don't assert that the orchestrator calls A then B. Assert on outcomes.
- **Fake at the adapter boundary**, never inside the domain.
- **Table-driven domain tests**: one table of `(input → expected)` per rule beats many bespoke tests.
- **Write domain tests as you write domain functions** (new code) or **before extracting them** (migration), as a safety net.
- **Determinism**: inject time/randomness/IDs; no sleeping, no network in unit tests.
- Test pyramid shape: many domain, fewer integration, very few end-to-end.

---

## Optional Exploratory Artifacts

Component catalogs, API explorers, REPL notebooks, example scripts, fixtures galleries: useful for **manual verification and communication**, not a substitute for automated tests.

Create one when mocking the feature's inputs is easy. Skip it when the setup cost outweighs the insight.

---

## Best Practices

- **Name by intent.** `canRetry`, `toInvoiceRequest`, not `helper1`.
- **Prefer data over callbacks** between layers. Data is easier to test and log.
- **Make illegal states unrepresentable** in contracts where the language allows.
- **Split by responsibility, not by size.** A long file with one responsibility is fine; a short file mixing two is not.
- **Split further when** a part has two reasons to change, or tests need unrelated fakes to exercise it.
- **Memoize/cache deliberately** and only at adapters or derived-state boundaries, never to hide impurity.
- **Co-locate** a feature's files; share only what's genuinely shared.
- **Don't abstract early.** Wait for the second use before extracting a shared piece.

---

## When to Skip a Role

| Situation                                         | Skip / merge                                              |
| ------------------------------------------------- | --------------------------------------------------------- |
| Trivial CRUD with no business rules               | Domain can be empty; keep contracts + adapters + entry    |
| Stateless request/response                        | No State role                                             |
| One action                                        | Fold Actions into the Orchestrator (still keep it logic-free of rules) |
| Pure computation library                          | Only Contracts + Domain (+ tests)                         |
| Script run once                                   | Likely none; don't ceremony a throwaway                   |
| Framework mandates a structure (controllers, modules, hooks) | Map roles onto the framework's units instead of adding files |

**The principles are mandatory. The file count is not.**

---

## Examples Across Stacks

### HTTP Service (illustrative)

```
orders/
├── contracts        # CreateOrderRequest, OrderResponse, Order
├── domain           # validateOrder, priceOrder, canCancel   (pure)
├── adapters         # OrderRepository, PaymentGateway
├── actions          # createOrder, cancelOrder               (use-cases)
├── orchestrator     # OrderService (wires repo + gateway + actions)
├── entry            # POST /orders, DELETE /orders/:id       (controllers)
├── serialization    # toOrderResponse
└── tests            # domain (table-driven) > actions > integration > e2e
```

### CLI Tool

```
sync/
├── contracts        # SyncOptions, SyncReport
├── domain           # diffTrees, planOperations              (pure)
├── adapters         # LocalFs, RemoteClient
├── actions          # runSync
├── entry            # main(): parse args → runSync → print
└── serialization    # formatReport
```

### Background Job

```
reconcile/
├── contracts        # ReconcileMessage, Discrepancy
├── domain           # findDiscrepancies, classify            (pure)
├── adapters         # LedgerStore, QueueClient
├── actions          # reconcileBatch
├── entry            # consumer(message) → reconcileBatch → ack/nack
└── tests
```

---

## Summary

### Pattern Principles

1. **Business logic in pure functions.**
2. **Actions orchestrate, they don't decide.**
3. **I/O behind adapters; hide the handles.**
4. **Orchestrators and entry points are boring.**
5. **Dependencies flow one way.**
6. **Contracts first.**
7. **Test the domain first.**

### Benefits

- Rules are testable in milliseconds with no infrastructure.
- Frameworks and datastores become replaceable details.
- Reviews get easier: each file has one kind of change.
- The same mental model carries across stacks and tiers.

### Companion Guides

- [ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md) - when the pattern doesn't map 1:1
- [QUICK_START_NEW_FEATURE.md](./QUICK_START_NEW_FEATURE.md)
- [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md)
- [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md)
