# Component Organization Patterns - Quick References

**Condensed guides for efficient day-to-day development.**

These quick references extract the essential patterns from the comprehensive [Component Organization Convention](./COMPONENT_ORGANIZATION_CONVENTION.md) document.

---

## Which Guide Should I Use?

### 🚀 [QUICK_START_NEW_COMPONENT.md](./QUICK_START_NEW_COMPONENT.md)

**Use when:** Building a new feature from scratch

**What you'll learn:**

- Step-by-step process: types → domain → hooks → components
- Code templates for each file
- Testing approach
- When to create Storybook stories

**Time to read:** 5-10 minutes

---

### 🔄 [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md)

**Use when:** Refactoring existing code to follow the pattern

**What you'll learn:**

- Differences between creating new vs migrating
- Incremental refactoring steps
- How to extract business logic from existing code
- Common pitfalls and how to avoid them

**Time to read:** 5-10 minutes

---

### ⚡ [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md)

**Use when:** You need a quick reminder of the patterns

**What you'll learn:**

- File structure diagram
- Core principles (10 commandments)
- Code templates
- Decision trees
- Quick checks for good/bad patterns

**Time to read:** 2-3 minutes

---

## Key Principles (Memorize These)

1. **Business logic → domain functions** (pure, testable)
2. **Actions curry domain functions** (no business logic in actions)
3. **Main hook is boring** (just wiring)
4. **Container is boring** (calls hook, renders presentation)
5. **Domain tests are highest priority** (then hooks, then integration, then component/interaction/screenshot)
6. **Storybook is optional** (skip if mocking is complex) and is used as a **manual verification/QA catalog** for components, not an automated test suite - automated interaction coverage lives in `.component.spec.tsx` files and pixel-level regression in `.screenshot.spec.tsx` files (see [COMPONENT_ORGANIZATION_CONVENTION.md § Testing Strategy](./COMPONENT_ORGANIZATION_CONVENTION.md#testing-strategy))

**Remember:** If you know these 6 principles, you can derive the rest.

---

## Recommended Usage in Work Sessions

### Scenario 1: Starting a New Feature

```text
Read docs/patterns/QUICK_START_NEW_COMPONENT.md and help me build
a user profile editor component with form validation and avatar upload.
```

### Scenario 2: Refactoring Existing Code

```text
Read docs/patterns/QUICK_START_MIGRATION.md and help me refactor
the AlertConfigurationDialog component to follow the pattern.
```

### Scenario 3: Quick Pattern Lookup

```text
Read the "Actions Hook Pattern" section from docs/patterns/QUICK_REF_PATTERNS.md
and help me fix this action handler.
```

### Scenario 4: Just Need a Reminder

```text
Check QUICK_REF_PATTERNS.md - where should validation logic go?
```

---

## When to Read the Full Document

Read [COMPONENT_ORGANIZATION_CONVENTION.md](./COMPONENT_ORGANIZATION_CONVENTION.md) when:

- **Onboarding new team members** - Comprehensive understanding
- **Understanding the "why"** - Full rationale and benefits explained
- **Complex scenarios** - Edge cases and advanced patterns
- **Need detailed examples** - Multiple complete implementations
- **Writing your own patterns** - Understanding the principles deeply

**Context usage:** the quick references are each roughly 4-6x shorter than the full convention doc - start there for 90% of cases and only pull in the full doc when you need the "why," not just the "how."

---

## Pattern Evolution

These quick references are **derived from** the main convention document. When the main document is updated:

1. Review changes in [COMPONENT_ORGANIZATION_CONVENTION.md](./COMPONENT_ORGANIZATION_CONVENTION.md)
2. Update quick references if patterns change
3. Keep quick references focused on "how" (actionable)
4. Keep main doc focused on "why" (explanatory)

---

## Feedback

These quick references are living documents. If you find:

- Missing information that would be helpful
- Sections that are too verbose
- Patterns that could be clearer

Please update them! The goal is **maximum utility with minimum context usage**.
