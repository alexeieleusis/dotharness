# Quick Start: Migrating Existing Code (Generic)

**Use this guide when refactoring existing code toward the pattern, in any stack.**

**Before you start:** map the roles to the codebase's own units ([ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md)). Migration should make the code look *more like its best neighbors*, not like a foreign architecture.

---

## Key Differences from Creating New

| Aspect             | Creating New                              | Migrating Existing                                   |
| ------------------ | ----------------------------------------- | ---------------------------------------------------- |
| **Starting point** | Clean slate                               | Working code with implicit logic                     |
| **Approach**       | Bottom-up (contracts → domain → ...)      | Characterize → extract → isolate → thin               |
| **Testing**        | Write tests as you build                  | Pin existing behavior first, then refactor under it  |
| **Risk**           | Low                                       | Medium: must preserve behavior                       |
| **Speed**          | Faster                                    | Slower: must understand first                        |
| **Exploratory artifacts** | Evaluate early                     | Usually skip until the core refactor is done         |

**Migration is exploratory** (discovering what the code does while reorganizing). **New development is constructive.**

---

## Principles

- **Behavior-preserving.** A migration PR changes structure, not behavior. Fix bugs separately.
- **Incremental.** The code works after every step. Each step is independently shippable.
- **Characterize before you change.** Pin current behavior with tests first.
- **Extract before you rewrite.** Move logic as-is, then improve.
- **Domain first.** Pull rules out before rearranging anything else.
- **Keep the public interface stable** while internals change.
- **Scope tightly.** Migrate the code you're touching, not the whole module.

---

## Step-by-Step Process

### Step 0: Characterize

Understand and pin the current behavior.

1. Read the code path end to end. Note triggers, inputs, outputs, side effects.
2. List the **implicit business rules** hiding in conditionals, loops, and string manipulations.
3. If tests exist, run them and note coverage gaps. If no tests exist, write **characterization tests** at the highest practical level (integration or entry-point). These tests capture *current* outputs, including odd ones.
4. Record anything surprising. Don't fix it yet.

```
characterization test:
    given  <representative input>
    when   <entry point invoked>
    then   <exactly what happens today>      // even if it looks wrong
```

---

### Step 1: Centralize Contracts

Gather scattered type definitions or shapes into one place, named for their role (input, output, entities, dependency interfaces).

- Behavior does not change. Only locations and names move.
- **Check:** the code compiles and lints. The characterization tests still pass.

---

### Step 2: Extract Business Logic into Domain Functions

**Look for:**

- Inline validation (`if (!x || x.trim() === '')`)
- Format conversions (form → request, row → entity, event → command)
- Business predicates (can delete? eligible? expired?)
- Calculations, derived values, thresholds, magic numbers
- Branching on "type"/"status"/"plan" that encodes policy

**Before:**

```
handleSave():
    if not form.name or form.name.strip() == "":
        notify("Name is required"); return
    req = { name: form.name.strip(), qty: form.qty }
    api.create(req)
```

**After:**

```
// domain
validate(form)   -> { valid, errors[] }
toRequest(form)  -> Request

// action
handleSave():
    v = domain.validate(form)
    if not v.valid: v.errors.each(notify); return
    api.create(domain.toRequest(form))
```

Process (one rule at a time):

1. Copy the logic into a new pure function, parameterizing anything impure (time, config, IDs).
2. Write a **domain test** for it, covering the cases the old code handled.
3. Replace the inline logic with a call.
4. Run characterization tests.

Commit after each extraction if it helps review.

---

### Step 3: Isolate I/O Behind Adapters

Find direct calls inside code that contains business rules. They target the database, network, filesystem, clock, environment, or framework globals.

1. Create (or reuse) an adapter exposing simple operations.
2. Move the call behind it. Translate external shapes to your contracts.
3. Inject the adapter into the code that used the raw call.
4. Add an in-memory fake. Use it in unit tests.

**Check:** domain functions no longer use I/O directly. The characterization tests still pass.

---

### Step 4: Split Mixed Responsibilities

Split large units *by responsibility*, in dependency order:

1. **State**: extract lifetime state and derived values. Compute derived values through the domain.
2. **Actions**: extract use-case steps ("validate → transform → save → notify").
3. **Orchestrator**: leave one boring unit that composes the others.

Don't split just to split. If a unit has one reason to change, leave it.

---

### Step 5: Thin the Entry Point

Reduce the entry point (controller / handler / component / command / consumer) to **parse → delegate → respond**.

- Move any remaining rules to the domain.
- Move direct adapter calls into actions.
- Move formatting into presentation/serialization.

**Check:** a reader can understand the entry point quickly. No business `if`s remain.

---

### Step 6: Add Layered Tests

Now that seams exist:

1. Domain tests for every extracted rule (already added in Step 2).
2. Action tests with fake adapters.
3. Integration test for the orchestrator.
4. Keep or trim the characterization tests. Keep the ones that still express meaningful end-to-end behavior. Delete the redundant ones.

---

### Step 7: Clean Up

- Remove dead code and now-unused helpers.
- Update documentation and comments that described the old structure.
- Ensure naming matches codebase conventions.
- Optionally add an exploratory artifact (catalog, example) if it's cheap now.

---

## Migration Checklist

- [ ] Pin behavior with tests **before** refactoring
- [ ] Each step leaves the code working
- [ ] Extract rules into pure, tested functions
- [ ] I/O is behind adapters. External shapes don't leak
- [ ] Entry point and orchestrator contain no rules
- [ ] No new cyclic or backwards dependencies
- [ ] No unrelated behavior changes or incidental fixes in the same PR
- [ ] Public interface unchanged (or the change is stated explicitly)

---

## Common Pitfalls

| Pitfall                                         | Fix                                                                 |
| ----------------------------------------------- | ------------------------------------------------------------------- |
| Big-bang rewrite                                | Follow the list above. Ship each step                               |
| Refactoring without tests                       | Characterize first                                                  |
| "Fixing" odd behavior mid-extraction            | Record it, extract faithfully, fix in a separate change             |
| Extracting a function that still does I/O       | Parameterize it or move the I/O to an adapter                       |
| Creating anemic layers that just forward calls  | Merge layers that have no distinct reason to change                 |
| Migrating unrelated code "while you're there"   | Limit scope to what the current task touches                        |
| Imposing a new structure over local conventions | Map roles onto what exists ([ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md)) |
| Breaking callers with repeated interface changes | Keep the public interface stable. Deprecate, then migrate callers   |

---

## When to Stop

Stop when:

- Rules are tested in isolation.
- The entry point and wiring are boring.
- The next step would add structure without adding testability or clarity.

Partial migration that delivers testable domain logic is a **success**, not a failure.

See [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md) for templates and [CODE_ORGANIZATION_CONVENTION.md](./CODE_ORGANIZATION_CONVENTION.md) for the rationale.
