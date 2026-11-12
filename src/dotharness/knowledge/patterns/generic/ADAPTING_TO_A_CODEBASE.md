# Adapting the Patterns to a Codebase

**Use this when the generic patterns don't obviously fit, when you're new to a repo, or when you're authoring a pattern set for a new framework.**

The generic roles ([Contracts, Domain, Adapters, State, Actions, Orchestrator, Entry point, Presentation](./CODE_ORGANIZATION_CONVENTION.md#the-roles)) are a **lens**. Find how the local codebase already expresses them. Then follow the local idiom and preserve the principles.

---

## The Rule of Three Layers

When principles and local convention disagree, resolve in this order:

1. **Repo instructions and lint/CI rules** (`CLAUDE.md`, `AGENTS.md`, contributing docs, architecture decision records)
2. **Established local convention** (what the neighbouring, healthy code does)
3. **These generic principles**

Never impose a structure the codebase doesn't use if a local equivalent exists. Never copy a local anti-pattern just because it is local. *Follow convention for structure. Follow the principles for logic placement.*

---

## Step 1: Discover (Before Writing Anything)

Spend a few minutes reading, not coding. Answer these questions **with evidence** (file paths), not guesses.

### Structure

- [ ] How is the repo organized: by layer (`controllers/`, `services/`), by feature (`orders/`, `billing/`), or hybrid?
- [ ] Where do tests live, and how are they named and run?
- [ ] Is there a documented architecture (README, `docs/`, ADRs, diagrams)?
- [ ] Is there an existing pattern set under `patterns/` for this framework?

### Roles: find each one's local home

| Role                      | Ask                                                         | Found at |
| ------------------------- | ----------------------------------------------------------- | -------- |
| Contracts                 | Where are request/response/entity types or schemas defined? |          |
| Domain                    | Is there pure logic anywhere? Where do validators/policies live? |     |
| Adapters                  | Where do DB/HTTP/FS/queue calls happen?                     |          |
| State                     | Is there per-request/per-view/per-job state? Where?         |          |
| Actions                   | What are the "verbs" (use-cases, service methods, handlers, commands)? | |
| Orchestrator              | What composes them (DI container, module, facade, main hook)? |        |
| Entry point               | What does the framework call first (route, component, command)? |      |
| Presentation/Serialization| Where is output formatted?                                  |          |

### Health Signals

Find the **best** example of a feature in the repo (well-tested, recently touched, reviewed carefully). Use it as the local template. Note any layer that the codebase consistently skips.

### Constraints

- [ ] Language/runtime features available (generics, interfaces, immutability, async model)
- [ ] Dependency injection mechanism, if any (framework container, constructor args, module imports, none)
- [ ] Test tooling and speed (can pure tests run without booting the framework?)
- [ ] Generated code, codegen'd types, or schema-first contracts that must not be hand-edited

---

## Step 2: Map the Roles

Write the mapping down (in your plan, PR description, or a comment to the user) before coding. Example:

```
Codebase: Django REST service
Contracts      → serializers.py (input), models.py (entities)
Domain         → NEW: <app>/domain.py (pure functions; nothing exists today)
Adapters       → <app>/repositories.py + clients/payments.py
State          → n/a (stateless request/response)
Actions        → <app>/services.py (one function per use-case)
Orchestrator   → folded into services.py
Entry point    → views.py (thin; calls services)
Serialization  → serializers.py (output)
```

### Mapping Heuristics

- **Framework unit ≈ role.** Sometimes the framework provides a unit that already has one responsibility (a "controller", a "reducer", a "use-case", a "hook", a "saga", a "resolver"). Use it for the closest role.
- **The framework's required classes are usually Entry points or Adapters.** Keep them thin.
- **Where the framework offers no home for pure logic, create one.** A plain module/file of functions with no framework imports is almost always possible.
- **Merge roles when the framework fuses them.** Keep the *logic placement* rule: rules still go in pure functions.
- **Active Record / ORM-heavy frameworks**: treat the model as Contracts + Adapter. Put rules in standalone domain functions that take plain data, not in model callbacks.
- **Declarative/config-driven frameworks** (schemas, DSLs, workflow definitions): the config is Contracts/Domain. Keep logic you'd otherwise embed in config as testable functions invoked from it, if the framework permits.
- **Functional vs. OO**: functions-and-data or classes-with-injected-collaborators are equally valid; pick the local style.

---

## Step 3: Decide What Is Non-Negotiable

Across every stack, hold these:

| Always                                                              | Negotiable                                         |
| ------------------------------------------------------------------- | -------------------------------------------------- |
| Business rules in pure, framework-free, I/O-free code               | File/folder names and suffixes                     |
| I/O isolated behind a seam you can fake                             | Number of layers                                   |
| Entry points and wiring contain no rules                            | Classes vs. functions                              |
| One-directional dependencies, no cycles                             | How DI is done                                     |
| Domain tests written first and fast                                 | Whether an exploratory catalog (stories, REPL) exists |
| Names and shapes centralized (contracts)                            | Whether orchestrator is a separate file            |

If the codebase makes a non-negotiable impossible (rare), say so explicitly. Note the deviation and its cost.

---

## Step 4: Apply Incrementally

- **Match the neighbours.** New code should look like the best nearby code, plus pure domain functions.
- **Introduce at most one new idea per change.** Adding a `domain` module is one idea; also restructuring directories is another PR.
- **Prove the seam with a test.** The first domain test is the evidence the extraction worked.
- **Leave the code a little cleaner, not rebuilt.** Don't migrate unrelated code unless asked (see [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md)).

---

## When the Patterns Genuinely Don't Apply

Some work isn't a "feature" in this sense. Adjust, don't force:

| Kind of work                          | What to do                                                              |
| ------------------------------------- | ----------------------------------------------------------------------- |
| Throwaway script / one-off migration  | Skip the structure; still keep decisions separate from I/O if it's cheap |
| Pure library / algorithm              | Contracts + Domain + tests only                                         |
| Infrastructure-as-code / config       | Keep modules single-purpose; validate with plan/lint rather than unit tests |
| Data pipeline / SQL transformations   | Treat each stage as Domain (pure transform) with I/O at the ends; test stages on fixtures |
| Glue code between two systems         | It *is* an adapter; keep it thin and contract-test it                   |
| Highly stateful UI/game loops         | Pure update/reducer functions are the Domain; the loop is the Entry point |
| Codebase with strong, different architecture (hexagonal, clean, DDD, MVC, ECS) | Map roles onto that architecture's terms; don't rename things |

If you can't map a role at all, **ask the user** rather than inventing structure. Present the discovery findings and a proposed mapping.

---

## Mapping Cheat Sheet by Common Architecture Style

| Style / framework family | Contracts          | Domain                  | Adapters                | Actions               | Orchestrator          | Entry point          |
| ------------------------ | ------------------ | ----------------------- | ----------------------- | --------------------- | --------------------- | -------------------- |
| Hexagonal / Clean        | DTOs, entities     | domain layer            | ports' implementations  | use-cases             | application service   | inbound adapters     |
| MVC server frameworks    | form/request objects, models | NEW pure module (not in model/controller) | models-as-persistence, clients | service layer / fat-controller extractions | service/facade | controllers |
| Component UI frameworks  | props/state types  | pure helper modules     | data-fetching units     | event handlers        | main hook/composable/service | container component |
| Redux/Elm-style          | state + action types | reducers & selectors  | middleware / effects    | action creators, thunks | store setup         | views/dispatchers    |
| Serverless functions     | event/response schemas | pure modules         | SDK clients             | handler body steps    | handler setup         | handler function     |
| Event-driven / CQRS      | commands, events   | decision functions (decide/evolve) | stores, buses | command handlers      | handler registry      | message consumers    |
| Data/ETL                 | row/record schemas | transform functions     | readers/writers         | stage runners         | pipeline definition   | scheduler/trigger    |

---

## Template: Authoring a New Framework-Specific Pattern Set

When adding `patterns/<framework>/`, produce the same family of files, each narrowed to the framework:

```
patterns/<framework>/
├── README.md                         # Which guide to use; key principles; usage prompts
├── <NAME>_ORGANIZATION_CONVENTION.md # Full rationale, framework-specific
├── QUICK_REF_PATTERNS.md             # Cheat sheet, templates, good/bad
├── QUICK_START_NEW_<UNIT>.md         # Greenfield walkthrough
└── QUICK_START_MIGRATION.md          # Refactor walkthrough
```

### Authoring Checklist

1. **Start from the generic files.** Copy the structure; replace pseudocode with real, idiomatic code from the framework.
2. **Fill the role mapping table** for this framework (Step 2 above) near the top of the convention doc.
3. **Use the framework's real units** (its component/controller/handler/module names) and conventional file layout and naming.
4. **Keep the principles identical** to the generic set. Framework sets may add *constraints and idioms*. They must not contradict principles. If a set must diverge, document why.
5. **Add a "Framework Gotchas" section.** Cover lifecycle traps, DI quirks, testing-harness setup, and things the framework makes hard (e.g. where pure logic can hide from the framework magic).
6. **Provide a minimal runnable example** that is a real, passing feature in that framework, with its domain test.
7. **Document test tooling**: how to run domain tests without booting the framework; how to fake adapters.
8. **Link back** to the generic set and state precedence ("framework conventions override generic when they conflict").
9. **Keep quick references 4-6x shorter** than the full convention.
10. **Validate with a dry run**: have someone (or an agent) build a small feature using only the new docs. Fix what confuses them.

### Section Skeleton for the Convention Doc

```
# <Framework> Organization Convention
## Goals / Non-Goals
## Role Mapping (generic role → framework unit)
## File Structure & Naming
## Contracts
## Domain Logic
## Adapters (framework-specific I/O seams)
## State
## Actions
## Orchestrator
## Entry Point
## Presentation / Serialization
## Dependency Flow
## Testing Strategy (tooling + commands)
## Framework Gotchas
## Creating New vs. Migrating
## Examples
## Summary
```

---

## Output to Give the User

When you apply this guide in a session, report briefly:

1. **What you found** (the local conventions, with file paths).
2. **The role mapping** you will use.
3. **Deviations** from the generic principles, and why.
4. **Open questions** you could not answer from the code.

Then proceed with [QUICK_START_NEW_FEATURE.md](./QUICK_START_NEW_FEATURE.md) or [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md) using the mapped names.
