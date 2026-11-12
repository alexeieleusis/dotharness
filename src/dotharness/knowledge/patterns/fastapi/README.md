# FastAPI Organization Patterns

**Framework-specific guidance for organizing Python/FastAPI features (Pydantic v2, `Depends`, async or sync).**

This set applies the [generic patterns](../generic/README.md) to FastAPI. It adds FastAPI idioms. It does not change the principles.

---

## Which Guide Should I Use?

### 🚀 [QUICK_START_NEW_ENDPOINT.md](./QUICK_START_NEW_ENDPOINT.md)

**Use when:** Adding a new feature/endpoint from scratch.

- Bottom-up: schemas → domain → ports/adapters → service → `Depends` wiring → router
- A complete, runnable `orders` example with its domain test

### 🔄 [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md)

**Use when:** Refactoring a fat route handler, a `crud.py`/god-service, or ORM-model-as-everything code.

- Characterize with `TestClient`, extract rules, isolate sessions, thin the router

### ⚡ [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md)

**Use when:** You need a reminder of the role map, templates, "what goes where", or good and bad examples.

### 📖 [FASTAPI_ORGANIZATION_CONVENTION.md](./FASTAPI_ORGANIZATION_CONVENTION.md)

**Use when:** You need the "why": role mapping, async/sync, session scoping, error mapping, gotchas, test tooling.

---

## Key Principles (identical to generic)

1. **Business logic lives in pure functions** (plain Python: no `fastapi`, no `pydantic` ORM-mode tricks, no `sqlalchemy`)
2. **Actions orchestrate; they don't decide** (service functions call domain + adapters)
3. **I/O sits behind adapters** (repositories, gateways, clock, id generator)
4. **Wiring is boring** (`Depends` providers and `create_app()` only pick implementations)
5. **Entry points are boring** (routers: parse, delegate, respond)
6. **Dependencies flow one way** (router → service → domain; never back)
7. **Test the domain first** (`pytest.mark.parametrize`, no app, no DB)

## FastAPI Idioms in One Glance

| Generic role | FastAPI unit |
| ------------ | ------------ |
| Contracts | Pydantic v2 `BaseModel` schemas (API) + domain dataclasses |
| Domain | `domain.py`: pure functions over frozen dataclasses |
| Adapters | Repository/gateway classes behind `typing.Protocol` ports |
| Actions | `service.py` functions, collaborators passed as keyword args |
| Orchestrator | `Depends` providers (`deps.py`) + `create_app()` + `lifespan` |
| Entry point | `APIRouter` path operation functions |
| Presentation | `response_model` + exception handlers |

---

## Recommended Usage in Work Sessions

```text
Read patterns/fastapi/QUICK_START_NEW_ENDPOINT.md and help me add a
"refunds" endpoint to this service.
```

```text
Read patterns/fastapi/QUICK_START_MIGRATION.md and help me refactor the
POST /orders handler so its pricing rules are testable without the app.
```

```text
Check patterns/fastapi/QUICK_REF_PATTERNS.md - does this validation belong in
the Pydantic schema or the domain?
```

---

## Precedence

1. **Explicit instructions and repo conventions** (`CLAUDE.md`, `AGENTS.md`, lint/CI rules, ADRs, the neighbouring healthy code)
2. **This FastAPI set**
3. **The [generic patterns](../generic/README.md)**

Repo conventions win over this set. Where this set and the generic patterns conflict on idiom, this set wins. If the repo already has a different layering (for example `routers/ services/ crud/ models/ schemas/`, or a hexagonal layout), map the roles onto it using [ADAPTING_TO_A_CODEBASE.md](../generic/ADAPTING_TO_A_CODEBASE.md). Do not rename things.

## Versions Assumed

This set assumes Python 3.10+, FastAPI with Pydantic v2, the `Annotated[..., Depends(...)]` style, pytest, and httpx/`TestClient`. Where a DB is mentioned, it assumes SQLAlchemy 2.x. The patterns also hold for SQLModel, Tortoise, raw drivers, or no DB at all.
