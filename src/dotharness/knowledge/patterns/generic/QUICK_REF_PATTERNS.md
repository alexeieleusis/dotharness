# Quick Reference: Code Organization Patterns (Generic)

**Condensed reference. Framework- and tier-agnostic.**

If the codebase uses a different vocabulary, map it first: [ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md).

---

## Feature Layout (Illustrative - Adapt Names to the Codebase)

```
feature-name/
├── contracts          # Types/schemas: input, output, internal shapes, dependency interfaces
├── domain             # Pure business rules (no I/O, no framework)
├── adapters           # DB / network / FS / queue / clock / UI-toolkit seams
├── state              # Optional: feature-lifetime state
├── actions            # Use-cases: call domain + adapters
├── orchestrator       # Wiring (boring)
├── entry-point        # Controller / handler / component / command (boring)
├── presentation       # Optional: view / serializer / formatter (pure)
└── tests/
    ├── domain         # HIGHEST PRIORITY
    ├── actions/adapters (unit, with fakes)
    ├── integration
    └── e2e            # Few
```

**Follow the codebase:** use files, folders, classes, or modules as it does.

---

## Dependency Flow

```
Contracts
  ↓
Domain          (pure)
  ↓
Adapters        (I/O)
  ↓
State           (optional)
  ↓
Actions         (use-cases)
  ↓
Orchestrator    (wiring)
  ↓
Entry point     (boundary)
  ↓
Presentation    (output)
```

Never point a dependency backwards. You may skip a layer.

---

## Core Principles (The 10 Commandments)

1. **Business logic → pure functions** (no I/O, no framework)
2. **Actions orchestrate, they don't decide** (call domain and don't contain rules)
3. **I/O behind adapters** (fakeable seams. External shapes don't leak inward.)
4. **Wiring is boring** (no logic in the orchestrator)
5. **Entry points are boring** (translate in → delegate → translate out)
6. **Presentation is pure** (output = f(result))
7. **Domain tests first** (then actions, integration, e2e)
8. **Hide handles** (never expose raw connections/transactions/mutation objects)
9. **Linear dependencies** (each part depends only on earlier ones)
10. **Inject the impure** (time, randomness, IDs, config passed in)

---

## Pattern Templates (Language-Neutral Pseudocode)

### Domain

```
// Pure. No I/O. Returns data describing the outcome.
function validate(input) -> { valid, errors[] }
function toRequest(input, context) -> Request
function canDelete(entity, related) -> { allowed, reason? }
function price(order, now, rules) -> Money     // 'now' injected, not read
```

### Action

```
function createThing(ctx):
    v = domain.validate(ctx.input)
    if not v.valid: return Failure(v.errors)

    req = domain.toRequest(ctx.input, ctx.data)
    out = ctx.adapters.things.create(req)      // effect via adapter
    ctx.state.markCreated(out)                 // optional
    return Success(out)
```

### Adapter

```
interface ThingStore:
    create(req) -> Thing
    find(id)    -> Thing | NotFound
// Implementation translates external <-> contracts and maps failures to error kinds.
// Callers never see the driver, connection, or transaction.
```

### Orchestrator (Composition Root)

```
function buildFeature(deps, input):
    state   = makeState(input)
    data    = deps.adapters.load(input)
    actions = makeActions({ input, state, data, adapters: deps.adapters })
    return { state: publicView(state), data, actions }   // internals stripped
```

### Entry Point

```
function handle(externalRequest):
    input  = parseAndAuthorize(externalRequest)    // translate in
    result = feature(input)                         // delegate
    return present(result)                          // translate out
```

---

## Decision Trees

### Where does this logic go?

```
Is it a business rule / decision / transformation?
├─ Yes → Domain (pure function)
└─ No
   ├─ Does it touch the outside world? → Adapter
   ├─ Does it sequence steps / respond to a trigger? → Action
   ├─ Does it hold changing values for the feature's lifetime? → State
   ├─ Does it just connect parts? → Orchestrator
   ├─ Does it receive the external trigger? → Entry point
   └─ Does it format output? → Presentation / Serialization
```

### Creating new or migrating?

```
Do I have existing code for this behavior?
├─ No  → New feature: bottom-up (contracts → domain → adapters → actions → wiring → entry)
└─ Yes → Migration: characterize → extract domain → isolate I/O → thin the entry point
```

### Does this pattern fit the codebase?

```
Can I name the local home of each role? (see ADAPTING_TO_A_CODEBASE.md)
├─ Yes → Use local names; follow the principles
├─ Mostly → Merge/skip roles; keep logic placement rules
└─ No → Discover more; ask the user; don't invent structure
```

### Should I add a layer?

```
Does it have a distinct reason to change AND distinct tests?
├─ Yes → Separate it
└─ No  → Fold it into its neighbour
```

---

## Testing Priority

1. **Domain tests** (HIGHEST): pure, instant, table-driven
2. **Action / adapter unit tests**: collaborators faked
3. **Integration tests**: real internal parts, fake external edges
4. **Contract tests**: adapters vs. real/recorded external system
5. **End-to-end**: a few critical journeys
6. **Presentation tests** (optional): snapshot/golden/visual where format matters

Exploratory artifacts (component catalogs, REPL notebooks, API explorers) are **manual aids**, not automated tests.

---

## Quick Checks

### ✅ Good Patterns

```
// ✅ Domain is pure; time is passed in
function isExpired(token, now) { return token.expiresAt <= now }

// ✅ Action reads as "call, call, call"
createThing(ctx):
    v = domain.validate(ctx.input); if !v.valid: return fail(v)
    return ctx.adapters.things.create(domain.toRequest(ctx.input))

// ✅ Entry point is thin
handle(req): return present(feature(parse(req)))

// ✅ Adapter hides its driver
things.create(req)            // not things.db.transaction(...)
```

### ❌ Anti-Patterns

```
// ❌ Domain reads the clock / env / DB
function isExpired(token) { return token.expiresAt <= Date.now() }

// ❌ Rule inside an action or entry point
if user.plan == "free" and count > 3: reject()      // belongs in domain

// ❌ Entry point talks to the database directly
handle(req): row = db.query(...); return render(row)

// ❌ Orchestrator has behavior
if flagX: skipValidation()                           // decision → domain

// ❌ Adapter leaks external shapes inward
return rawVendorResponse                             // translate to contracts

// ❌ Cycles / backwards imports
domain imports adapters
```

---

## Cheat Sheet: What Goes Where

| Concern               | Role           | Contains                                   | Pure? | I/O? |
| --------------------- | -------------- | ------------------------------------------ | ----- | ---- |
| Shapes & interfaces   | Contracts      | Types, schemas, interfaces                 | n/a   | No   |
| Business rules        | Domain         | Validation, transforms, predicates, calcs  | ✅    | No   |
| External systems      | Adapters       | Drivers, clients, translation, retries     | ❌    | ✅   |
| Lifetime state        | State          | Flags, progress, derived values            | ❌    | No   |
| Use-cases             | Actions        | Sequencing of domain + adapter calls       | ❌    | Via adapters |
| Composition           | Orchestrator   | Constructing/wiring parts                  | ❌    | No   |
| External trigger      | Entry point    | Parse, delegate, respond                   | ❌    | Edge |
| Output formatting     | Presentation   | Render/serialize/format                    | ✅    | No   |

---

## When Stuck

1. **"Where does this logic go?"** → Is it a decision? Domain. Does it touch the outside? Adapter. Otherwise, use the decision tree above.
2. **"The framework forces a structure."** → Map each role to one of its units. Keep rules in pure functions. See [ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md).
3. **"It's hard to test."** → Something impure is inside the domain. Move it to an adapter or inject it.
4. **"This part is getting too big."** → Split by *reason to change*, not by line count.
5. **"Creating or migrating?"** → New → bottom-up. Existing → characterize, then extract.
6. **"Nothing here fits."** → Discover, then ask. Don't invent structure.
