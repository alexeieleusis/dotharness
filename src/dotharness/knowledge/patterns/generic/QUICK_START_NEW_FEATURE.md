# Quick Start: Creating a New Feature (Generic)

**Use this guide when building a new feature from scratch, in any stack.**

**Before you start:** map the roles to the codebase's own units. See [ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md). Everywhere below, "file" means "whatever the codebase uses for a unit": file, module, class, package.

**Note on exploratory artifacts:** component catalogs, REPL notebooks and API explorers are manual aids. Automated coverage comes from the tests in [Step 8](#8-write-the-remaining-tests).

---

## What You'll Create

```
feature-name/
├── contracts          # shapes & interfaces
├── domain             # pure business rules
├── adapters           # I/O seams (or reuse existing ones)
├── state              # optional
├── actions            # use-cases
├── orchestrator       # wiring
├── entry-point        # boundary (route / command / component / consumer)
├── presentation       # optional output formatting
└── tests/
    ├── domain         # HIGHEST PRIORITY, written alongside domain
    ├── actions / adapters
    └── integration
```

Skip any role the feature does not need ([When to Skip a Role](./CODE_ORGANIZATION_CONVENTION.md#when-to-skip-a-role)).

---

## Step-by-Step Process

### 0. Clarify the Behavior

Write down, in plain language:

- **Trigger**: what starts this? (request, click, command, message, schedule)
- **Inputs & outputs**
- **Rules**: every "if/when/unless" a stakeholder would recognize
- **Side effects**: what does it read/write/notify?
- **Failure modes**: what can go wrong, and what should the caller see?

If the rules are unclear, ask before coding. Rules are the part that's expensive to get wrong.

---

### 1. Define Contracts First

```
Input            : what the entry point accepts (after parsing)
Output           : what the feature returns
Entities/values  : internal data shapes
Dependencies     : interfaces the adapters must satisfy
```

Use progressive composition: each later step takes the original input plus earlier results.

```
ActionsInput = Input + { state, data, adapters }
```

If the codebase generates types from a schema (OpenAPI, protobuf, GraphQL, DB), **change the schema and regenerate** the types. Do not write them by hand.

---

### 2. Write the Domain (Pure)

**PURE FUNCTIONS ONLY**: no I/O, no framework imports, no hidden clock/random/env.

```
function validate(input) -> { valid, errors[] }
    errors = []
    if isBlank(input.name): errors.add("name is required")
    if input.quantity <= 0: errors.add("quantity must be positive")
    return { valid: errors.isEmpty(), errors }

function toRequest(input, entities) -> Request
    return { name: trim(input.name), quantity: input.quantity,
             refs: entities.map(e => e.id) }

function canDelete(entity, related) -> { allowed, reason? }
    if related.nonEmpty(): return { allowed: false, reason: "has related items" }
    return { allowed: true }
```

Rules of thumb:

- Return **outcome data** (with reasons), not booleans-and-side-effects.
- Pass `now`, IDs, and randomness **in**.
- One rule per function, named for the rule.

**Write the domain tests now** (Step 3).

---

### 3. Test the Domain Immediately

Table-driven, no setup, no fakes:

```
cases = [
  ("blank name",      {name: " ",  quantity: 1},  invalid("name is required")),
  ("zero quantity",   {name: "a",  quantity: 0},  invalid("quantity must be positive")),
  ("valid",           {name: "a",  quantity: 2},  valid),
]
for each case: assert validate(case.input) == case.expected
```

Cover: happy path, each rule's boundary, empty/null/edge input, every `reason` a function can return.

---

### 4. Build or Reuse Adapters

- **Search first.** Does an adapter for this resource already exist? Reuse it. Extend it only if needed.
- A new adapter exposes **simple operations**, not driver handles:

```
interface ThingStore:
    create(req) -> Thing
    list(filter) -> Thing[]
    delete(id)   -> void
```

- Translate external representations to your contracts at this boundary.
- Map external failures to a small set of error kinds the actions understand.
- Provide an **in-memory fake** for tests if the codebase doesn't already have one.

---

### 5. Build State (If Needed)

Use state only for features that keep state across their lifetime (UI flags, request context, job progress).

```
state = { editingId: null, isSubmitting: false }
derived = domain.computeSummary(state, data)       // derived values come from domain

// Internal view includes mutators for actions; public view is read-only.
```

Stateless request/response features skip this step.

---

### 6. Build Actions

Each action reads as **"call, call, call"**:

```
function create(ctx):
    v = domain.validate(ctx.input)
    if not v.valid:
        return Failure(v.errors)

    req = domain.toRequest(ctx.input, ctx.data.entities)
    result = ctx.adapters.things.create(req)

    ctx.state.markCreated(result)         // if state exists
    return Success(result)
```

If an action contains branching that encodes a business rule, move that branch to the domain.

---

### 7. Wire the Orchestrator and the Entry Point

**Orchestrator** (boring):

```
function buildFeature(deps, input):
    state   = makeState(input)
    data    = load(deps.adapters, input)
    actions = makeActions({ input, state, data, adapters: deps.adapters })
    return { state: publicView(state), data, actions }
```

**Entry point** (boring):

```
function handle(externalRequest):
    input  = parseAndAuthorize(externalRequest)
    result = buildFeature(deps, input).actions.create(...)
    return present(result)
```

If the framework offers dependency injection, register the adapters and orchestrator there instead of hand-rolling `deps`.

---

### 8. Write the Remaining Tests

| Test                | What                                                         | Fakes                     |
| ------------------- | ------------------------------------------------------------ | ------------------------- |
| Action unit tests   | Outcomes for success, each validation failure, adapter error | Fake adapters             |
| Adapter tests       | Translation & error mapping                                  | Recorded/real external    |
| Integration test    | Orchestrator with real internal parts                        | Fake external edges only  |
| Entry-point test    | Parsing, status/exit codes, response mapping                 | Fake orchestrator or edges |
| End-to-end (few)    | A critical journey through the real stack                    | None / test environment   |

Assert **outcomes**, not call sequences.

---

### 9. Optional: Exploratory Artifact

If mocking inputs is easy, add a catalog/example/notebook so people can inspect the feature's states manually. Skip it if setup cost exceeds the value.

---

## Checklist Before Opening a PR

- [ ] Every business rule lives in a pure function with a test
- [ ] Actions contain no rules; entry point and orchestrator contain no logic
- [ ] No adapter handles/external shapes leak past the adapter
- [ ] No backwards or cyclic dependencies
- [ ] Time/randomness/IDs injected into domain functions
- [ ] Follows the codebase's naming, layout, and lint rules
- [ ] Tests run without booting the whole framework (at least the domain tests)
- [ ] Generated artifacts regenerated, not hand-edited

---

## Common Pitfalls

| Pitfall                                         | Fix                                                        |
| ----------------------------------------------- | ---------------------------------------------------------- |
| Starting with the entry point                   | Start with contracts and domain. Build the entry point last |
| Domain function reads the clock/env/DB          | Inject the value or move the read into an adapter          |
| "Utility" module that mixes rules and I/O       | Split: rules → domain, I/O → adapter                       |
| Over-layering a trivial feature                 | Skip roles; keep principles                                |
| Duplicating an existing adapter                 | Search for and reuse the existing one                      |
| Mirroring DB/vendor shapes everywhere           | Translate at the adapter into your own contracts           |
| Inventing structure the codebase doesn't use    | Re-read [ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md) |

See [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md) for templates and decision trees, and [CODE_ORGANIZATION_CONVENTION.md](./CODE_ORGANIZATION_CONVENTION.md) for the rationale.
