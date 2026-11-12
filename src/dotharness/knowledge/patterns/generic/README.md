# Generic Code Organization Patterns

**Framework-agnostic, layer-agnostic guidance for organizing a feature.**

These guides distill the ideas behind the framework-specific pattern sets (e.g. [`../react/`](../react/README.md)). They use vocabulary that applies to any language, framework, or tier: frontend, backend service, CLI, worker, data pipeline, library.

Use them in two ways:

1. **As a fallback** - when no framework-specific pattern set exists for the codebase you are working in.
2. **As the source of truth for new framework sets** - when adding `patterns/<framework>/`, start from [ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md).

---

## Which Guide Should I Use?

### 🧭 [ADAPTING_TO_A_CODEBASE.md](./ADAPTING_TO_A_CODEBASE.md)

**Use when:** Starting in a codebase for the first time, or when the patterns "don't fit".

- How to discover the codebase's existing conventions first
- Mapping the generic roles (Domain, Actions, Adapters, ...) onto the local framework
- What to keep non-negotiable vs. what to adapt
- Template for authoring a new framework-specific pattern set

---

### 🚀 [QUICK_START_NEW_FEATURE.md](./QUICK_START_NEW_FEATURE.md)

**Use when:** Building a new feature from scratch.

- Bottom-up process: contracts → domain → adapters → actions → wiring → entry point
- Language-neutral pseudocode templates
- Testing approach per step

---

### 🔄 [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md)

**Use when:** Refactoring existing code toward the pattern.

- Incremental, behavior-preserving extraction steps
- How to find and extract business logic from tangled code
- Common pitfalls

---

### ⚡ [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md)

**Use when:** You need a quick reminder.

- Roles and dependency flow
- Core principles
- Decision trees, good/bad checks, "what goes where" table

---

### 📖 [CODE_ORGANIZATION_CONVENTION.md](./CODE_ORGANIZATION_CONVENTION.md)

**Use when:** You need the "why": onboarding, edge cases, writing a new pattern set, or settling a design argument.

---

## Key Principles (Memorize These)

1. **Business logic lives in pure functions** (no I/O, no framework, trivially testable)
2. **Actions orchestrate. They don't decide** (they call domain functions and adapters)
3. **I/O sits behind adapters** (database, network, filesystem, clock, UI toolkit)
4. **Wiring is boring** (the orchestrator composes parts, contains no logic)
5. **Entry points are boring** (controller / handler / component / command: translate in, call, translate out)
6. **Dependencies flow one way** (entry point → orchestration → actions → domain; never back)
7. **Test the domain first** (then actions, then integration, then end-to-end)

If you know these 7 principles, you can derive the rest. You can also apply them in a codebase that none of these files mention.

---

## Recommended Usage in Work Sessions

```text
Read patterns/generic/ADAPTING_TO_A_CODEBASE.md, inspect this repo, and tell me
how the generic roles map onto it before we write any code.
```

```text
Read patterns/generic/QUICK_START_NEW_FEATURE.md and help me add an
"invoice export" feature to this service.
```

```text
Read patterns/generic/QUICK_START_MIGRATION.md and help me refactor
OrderController so its business rules are testable in isolation.
```

```text
Check QUICK_REF_PATTERNS.md - where should validation logic go?
```

---

## Precedence

When several sources of guidance apply, resolve conflicts in this order:

1. **Explicit instructions** from the user or the repo (`CLAUDE.md`, `AGENTS.md`, contributing docs, lint rules)
2. **The codebase's established conventions** (match what neighbouring code does)
3. **A framework-specific pattern set** (e.g. `patterns/react/`)
4. **These generic patterns**

The generic patterns are a lens, not a mandate. Never fight a codebase's working conventions to satisfy them.

---

## Pattern Evolution

These guides are the **abstraction layer**. Framework-specific sets are **instantiations** of it.

- Change a principle here → review each framework set for drift.
- Discover a framework-specific lesson → put it in that framework's set, not here.
- Keep this directory free of framework, language, and library names except where marked as an illustrative example.
