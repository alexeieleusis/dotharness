# FastAPI Organization Convention

**The comprehensive FastAPI reference. Read the quick guides first. Come here for the "why".**

This is the FastAPI instantiation of the [generic convention](../generic/CODE_ORGANIZATION_CONVENTION.md). The principles are identical. This document adds idioms, a role mapping, and gotchas.

**Precedence:** repo instructions and conventions > this set > generic. When the codebase already has a working layering, map onto it ([ADAPTING_TO_A_CODEBASE.md](../generic/ADAPTING_TO_A_CODEBASE.md)).

---

## Table of Contents

1. [Goals / Non-Goals](#goals--non-goals)
2. [Role Mapping](#role-mapping)
3. [File Structure & Naming](#file-structure--naming)
4. [Contracts](#contracts)
5. [Domain Logic](#domain-logic)
6. [Adapters](#adapters)
7. [State](#state)
8. [Actions](#actions)
9. [Orchestrator](#orchestrator)
10. [Entry Point](#entry-point)
11. [Presentation / Serialization](#presentation--serialization)
12. [Dependency Flow](#dependency-flow)
13. [Testing Strategy](#testing-strategy)
14. [Framework Gotchas](#framework-gotchas)
15. [Creating New vs. Migrating](#creating-new-vs-migrating)
16. [Examples](#examples)
17. [Summary](#summary)

---

## Goals / Non-Goals

**Goals**

- **Testability**: every business rule runs under plain `pytest`, no app, no DB, no event loop.
- **Changeability**: swapping SQLAlchemy for another store, or REST for a queue consumer, touches adapters and entry points, not rules.
- **Readability**: a newcomer can predict where a rule lives (`domain.py`) and where a route's work happens (`service.py`).
- **Small blast radius**: an API-shape change (schema) does not ripple into rules or persistence.

**Non-Goals**

- Prescribing exact folder names, or classes vs. functions.
- Adding layers a feature doesn't need (see [When to skip a role](../generic/CODE_ORGANIZATION_CONVENTION.md#when-to-skip-a-role)). A three-line health check needs no domain module.
- Replacing a repo's established structure.

---

## Role Mapping

| Generic role | FastAPI unit | Typical file | Pure? | I/O? |
| ------------ | ------------ | ------------ | ----- | ---- |
| **Contracts** (API) | Pydantic v2 `BaseModel` request/response schemas | `schemas.py` | n/a | No |
| **Contracts** (internal) | Frozen `@dataclass`es / `NamedTuple`s / enums; result types (`Order \| Rejection`) | `domain.py` (types at top) | n/a | No |
| **Domain** | Pure functions over internal contracts | `domain.py` | Yes | No |
| **Adapters** | Repositories, gateways, clock/id providers, behind `typing.Protocol` ports | `ports.py` (Protocols), `repository.py` / `adapters.py` / `gateways.py` | No | Yes |
| **State** | Request-scoped context, unit of work/session (via `Depends` with `yield`); app-scoped clients via `lifespan` / `app.state` | `deps.py`, `lifespan` in `main.py` | No | Resource handles |
| **Actions** | Service / use-case functions (one per use-case) | `service.py` | No | Via adapters |
| **Orchestrator** | `Depends` providers + `create_app()` + `lifespan`; `dependency_overrides` is its test seam | `deps.py`, `main.py` | No | No |
| **Entry point** | `APIRouter` path operation functions; also CLI commands, background-task callables, queue consumers | `router.py` | No | Edge only |
| **Presentation** | `response_model`, exception handlers, `status_code`s; pure mapper functions when non-trivial | `schemas.py`, `errors.py`, `presenters.py` | Yes | No |

Notes on the mapping:

- **ORM models (SQLAlchemy/SQLModel) are Adapter-internal.** They are persistence shapes, not contracts. They live beside the repository and never cross the repository boundary. (Repo-wide shortcut for CRUD-only features: see [When to skip a role](#when-to-skip-a-role-fastapi-flavor).)
- **Pydantic is a boundary tool.** It validates untrusted *shape* at the edge and serializes output. Domain code does not import it.
- **`Depends` is the orchestrator's mechanism**, not a place for rules. A dependency that decides business outcomes is a misplaced action.
- **Auth** (`Depends(current_user)`) is entry-point translation: it produces a plain `user_id`/principal that the router passes on. Authorization *rules* ("only owners may cancel") are domain functions taking that principal.

---

## File Structure & Naming

Prefer **package-per-feature** over package-per-layer (`routers/ services/ models/`), unless the repo already does the latter.

```
app/
├── main.py                 # create_app(), lifespan, include_router, handler registration
├── errors.py               # exception handlers (app-wide mapping)
├── core/                   # config (pydantic-settings), db engine/session factory, logging
│   ├── config.py
│   └── db.py
└── orders/                 # one package per feature/bounded context
    ├── schemas.py          # Pydantic v2 API contracts (in/out)
    ├── domain.py           # PURE: dataclasses + rule functions
    ├── ports.py            # typing.Protocol seams the service depends on
    ├── adapters.py         # repository/gateway implementations (+ ORM models in models.py)
    ├── service.py          # actions: call, call, call
    ├── deps.py             # Depends providers = the feature's composition root
    └── router.py           # APIRouter: thin entry points
tests/
└── orders/
    ├── test_domain.py      # parametrized, no fixtures needed (write FIRST)
    ├── test_service.py     # fakes for ports
    ├── test_adapters.py    # real DB (sqlite / testcontainers) for translation + queries
    └── test_api.py         # TestClient + dependency_overrides
```

Naming:

| Thing | Convention |
| ----- | ---------- |
| Schemas | `PlaceOrderIn`, `OrderOut` (suffix `In`/`Out`, or `Create`/`Read`/`Update`; follow the repo) |
| Domain functions | verbs/rules: `check_requested`, `bulk_discount_cents`, `decide_order` |
| Outcome types | `Order \| Rejection` (union of frozen dataclasses), or an `Enum` of reasons |
| Ports | nouns for the capability: `OrderRepository`, `PriceCatalog`, `PaymentGateway` |
| Services | verb per use-case: `place_order`, `cancel_order` (module of functions, or a class if the repo prefers) |
| Providers | `get_<thing>` in `deps.py`; `Annotated` aliases `OrdersDep = Annotated[OrderRepository, Depends(get_orders)]` |
| Routers | `router = APIRouter(prefix="/orders", tags=["orders"])` |

Shared things (db session provider, auth principal, pagination params) live in `core/` or a `shared/` package. Features use them and rarely redefine them.

---

## Contracts

Two families of contracts, deliberately separate.

### API contracts: Pydantic v2 schemas

```python
class LineIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0)


class PlaceOrderIn(BaseModel):
    customer_id: str = Field(min_length=1)
    lines: list[LineIn]


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)  # build from domain dataclass / ORM object
    id: str
    ...
```

- **Separate request and response models.** Use `OrderIn` and `OrderOut`, never one class for both. Inputs have no `id` or server-set fields. Outputs never echo secrets.
- **Separate from ORM models and from domain dataclasses.** Three shapes for three reasons to change (wire format, persistence, rules). Merge only when a feature is truly CRUD-only.
- Use `from_attributes=True` on output models so routers can return domain objects and let `response_model` serialize them.
- Version breaking changes (`OrderOutV2`) rather than mutating in place.
- If the repo generates clients from the OpenAPI schema, treat the schema as the contract: change schemas, regenerate clients.

### Internal contracts: domain dataclasses and Protocols

```python
@dataclass(frozen=True)
class Order:
    id: str
    customer_id: str
    lines: tuple[Line, ...]
    total_cents: int
    placed_at: datetime
```

- `frozen=True` dataclasses (or `NamedTuple`): immutable, hashable, trivially comparable in tests.
- Tuples over lists for collections inside frozen values.
- Money as integer minor units (or `Decimal`), never `float`.
- Dependency contracts are `typing.Protocol`s in `ports.py` (structural: fakes need no inheritance).

### Progressive composition

Each step takes the original input plus earlier results, as keyword arguments (`decide_order(order_id=..., requested=..., prices=..., now=...)`). The data flow is visible and linear. No step reaches into globals.

---

## Domain Logic

**Everything that encodes a business decision lives in pure Python.**

### What belongs

- Policy validation (limits, uniqueness, allowed state transitions)
- Calculations (totals, discounts, taxes, fees)
- Transformations between internal representations
- Predicates with reasons (`can_cancel(order, now) -> Allowed | Denied(reason)`)
- Authorization rules (given a principal)

### What does not

- `import fastapi`, `import sqlalchemy`, `from pydantic import ...` (including `HTTPException`)
- `datetime.now()`, `uuid4()`, `random`, `os.environ`, `open()`, `requests`/`httpx`
- Logging/metrics as a side channel for decisions
- Mutating arguments

A cheap, enforceable check: `grep -nE "^(from|import) (fastapi|pydantic|sqlalchemy|starlette)" app/*/domain.py` must be empty. Consider an import-linter / ruff `banned-api` rule to make it CI-enforced.

### Shape

```python
def decide_order(*, order_id, customer_id, requested, prices, now) -> Order | Rejection:
    reasons = check_requested(requested)
    if reasons:
        return Rejection(tuple(reasons))
    ...
```

- **Return outcome data** (`Order | Rejection`, `(value, reasons)`), not exceptions, for *expected* failures. Services turn rejections into exceptions/status codes.
- Raise only for programmer errors/invariant violations (`ValueError` on impossible input).
- **Time, ids, randomness are arguments** (`now: datetime`, `order_id: str`).
- Use `match`/`isinstance` on outcome unions in callers.

### Pydantic validation vs. domain validation

The most common FastAPI placement mistake. Rule of thumb:

| Question | Where |
| -------- | ----- |
| Is the payload well-formed? (types, required, ranges that are part of the *wire format*: `quantity > 0`, string length, enum membership, email shape) | **Pydantic schema** at the edge. Failures are automatic 422s. |
| Does it satisfy a *business* rule? (max 20 lines, no duplicate SKUs, sku exists, order within credit limit, state transition allowed) | **Domain function.** Failures are domain outcomes with reasons. |
| Does it depend on data you must fetch? | **Domain**, with the data passed in by the action. Never a Pydantic validator that calls a repository. |
| Could another entry point (CLI, queue consumer) skip the schema? | Then the rule must be in the domain, or it will be skipped too. |

Corollary: Pydantic `@field_validator`s stay *syntactic* (normalize case, strip whitespace, parse). If a validator contains a number a product manager would recognize, move it to the domain and keep a named constant there.

Two kinds of 422 may appear (Pydantic's `detail` array vs. your domain `reasons`). Decide once, repo-wide, whether to unify them (an exception handler for `RequestValidationError` can reshape it) and document it.

---

## Adapters

Adapters isolate everything outside the process: database, HTTP APIs, queues, files, cache, clock, id generation.

### Ports as `Protocol`s

```python
class OrderRepository(Protocol):
    def add(self, order: Order) -> None: ...
    def get(self, order_id: str) -> Order | None: ...
```

The service types its parameters against the Protocol; production passes the SQLAlchemy repository, tests pass an in-memory one. No inheritance, no mock library needed.

### Repository rules

- Expose **domain operations** (`add`, `get`, `list_for_customer`), not session handles, query builders or ORM objects.
- **Translate** ORM rows to domain dataclasses (`_to_domain(row)`) and back (`_to_row(order)`) inside the adapter. ORM models never cross the boundary.
- **Map external failures** to a few meaningful exceptions (`NotFound`, `Conflict`, `Unavailable`), e.g. `IntegrityError` -> `Conflict`.
- **Who commits?** The unit of work owns commit/rollback ([Session and transaction scoping](#session-and-transaction-scoping)); repositories `add`/`flush` only.
- Retries, timeouts, circuit breakers belong in gateways for remote APIs, configured via settings.

### Sync vs. async adapters

Match the stack: sync SQLAlchemy `Session` with `def` routes, or `AsyncSession` with `async def` routes. Ports can be sync or async; a service awaiting async ports is itself `async def`. The **domain stays sync regardless**: it is pure CPU, so there is nothing to await. See [Blocking calls in async endpoints](#blocking-calls-in-async-endpoints).

### Clock and ids

Small providers (`Callable[[], datetime]`, `Callable[[], str]`) injected via `Depends`. Domain receives the *values*; the service calls the providers. See [Time and uuid injection](#time-and-uuid-injection).

---

## State

Most FastAPI features are stateless request/response and **skip this role**. State that exists:

| Kind | Scope | Home |
| ---- | ----- | ---- |
| DB engine, HTTP client, ML model, connection pools | Process | Created in `lifespan`, stored on `app.state` (or a module-level singleton built by a provider), closed on shutdown |
| DB session / unit of work | Request | `Depends` generator (`yield`) |
| Current user/principal, request id, tenant | Request | `Depends` provider returning a plain value |
| Job progress, cache | Process/external | Adapter (Redis, table), not module globals |

- Keep it minimal; derive values with domain functions.
- **Never use mutable module globals for per-request data**: concurrent requests share the process. Process-wide caches are adapters with explicit invalidation.
- Public vs. internal surface: handlers receive read-only values (principal, settings); only adapters hold mutators (sessions).

### Lifespan

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(timeout=5)  # or engine = create_async_engine(...)
    yield
    await app.state.http.aclose()


app = FastAPI(lifespan=lifespan)
```

Use `lifespan` (not deprecated `@app.on_event`). Providers read from `request.app.state`. With `TestClient`, enter it as a context manager (`with TestClient(app)`) to run lifespan.

---

## Actions

Actions are the feature's use-cases: `place_order`, `cancel_order`. In FastAPI they are **service functions** (or methods of a small class when the repo prefers constructor injection).

```python
def place_order(customer_id, requested, *, orders, catalog, now, new_id) -> Order:
    prices = catalog.prices_for([l.sku for l in requested])  # adapter
    outcome = domain.decide_order(
        order_id=new_id(), customer_id=customer_id, requested=requested, prices=prices, now=now()
    )  # domain
    if isinstance(outcome, domain.Rejection):  # surface
        raise OrderRejected(outcome.reasons)
    orders.add(outcome)  # adapter
    return outcome
```

Rules:

- Read it as "call, call, call". The only branch is *did the domain accept?*
- **Collaborators are keyword-only arguments** (or constructor fields). Never `from app.db import session` inside a service.
- **Services take and return plain/domain values**, not `Request`, `Response`, `HTTPException`, or Pydantic input models. (Convert `PlaceOrderIn` to domain types in the router via `body.to_domain()`; see the note in [Entry Point](#entry-point).)
- Actions own *when* an error surfaces and in what shape: they raise **application exceptions** (`OrderRejected`, `OrderNotFound`) that handlers map to HTTP. Do not raise `HTTPException` from services: it couples them to the transport and breaks CLI/worker reuse.
- Services may call other services only downward or sideways, without cycles. Shared steps become domain functions or adapters.
- Async: `async def` service awaiting async ports, domain calls inline.

### Background tasks

`BackgroundTasks` (and Celery/ARQ/queue jobs) are **entry points for follow-up work**, not a place to hide rules.

- The request-time service decides and persists; the background callable is a thin function that calls another service function with its own adapters.
- Do **not** reuse the request's `Depends` session in a background task (it may already be closed). Open a fresh unit of work inside the task, or enqueue ids and let the worker build its own.
- Pass plain data (ids), not ORM objects or request-bound objects.

---

## Orchestrator

In FastAPI the orchestrator is **`Depends` + `create_app()` + `lifespan`**. It is the composition root: the only place that chooses concrete implementations.

```python
# deps.py: boring on purpose
def get_orders(session: SessionDep) -> OrderRepository:
    return SqlOrderRepository(session)


def get_now() -> Callable[[], datetime]:
    return lambda: datetime.now(timezone.utc)


OrdersDep = Annotated[OrderRepository, Depends(get_orders)]
```

- Providers construct adapters and return them typed as the **Protocol**, not the implementation.
- No conditionals that encode behavior; at most environment-based selection (settings), which is wiring.
- One provider per collaborator; compose with other `Depends` (the repository depends on the session dependency).
- `create_app()` factory (not just a module-level `app`) so tests build fresh apps: includes routers, registers exception handlers, attaches `lifespan`.
- `app.dependency_overrides[get_orders] = lambda: fake` is the **test seam**. Anything you want to fake must go through a provider.
- Use `Annotated` aliases to keep router signatures short and to give each dependency one canonical name.

Heuristic: if a reviewer must *think* to understand `deps.py`, a rule leaked into it.

---

## Entry Point

The path operation function: translate in, delegate, translate out.

```python
@router.post("", response_model=OrderOut, status_code=201)
def place_order(body: PlaceOrderIn, orders: OrdersDep, catalog: CatalogDep, now: NowDep, new_id: NewIdDep):
    return service.place_order(
        body.customer_id, body.to_domain(), orders=orders, catalog=catalog, now=now, new_id=new_id
    )
```

- **No rules, no direct adapter calls, no `if` encoding policy.** Status code, `response_model`, `responses=` docs, and the delegation are the whole job.
- Translating schema -> domain types happens here (a small `to_domain()` on the schema is acceptable because it is mapping, not a rule). If the mapping is non-trivial, put a pure mapper function in `presenters.py`/`mappers.py`.
- Auth: take `principal: PrincipalDep`, pass `principal.id` onward.
- **`def` vs. `async def`**: choose per the [async guidance](#blocking-calls-in-async-endpoints). The choice is the entry point's, not the domain's.
- The same service is reusable from a CLI (`typer`), worker, or test, because nothing in it knows about HTTP.
- Other entry points (CLI command, queue consumer, scheduled job) follow the same shape: parse, call the service with adapters built by the composition root, report.

---

## Presentation / Serialization

- **`response_model`** is the output filter: it guarantees only `OrderOut` fields leave, even if the handler returns a richer object. Always set it (or a return annotation with a model) on routes returning domain/ORM data.
- Return **domain dataclasses** from handlers with `from_attributes=True` models, or build `OrderOut` explicitly. Never return ORM instances.
- **Error mapping via exception handlers** (`errors.py`), registered in `create_app()`:

  ```python
  @app.exception_handler(OrderRejected)
  async def _rejected(_: Request, exc: OrderRejected):
      return JSONResponse(status_code=422, content={"detail": "order rejected", "reasons": exc.reasons})
  ```

  | Layer | Owns |
  | ----- | ---- |
  | Domain | *what* the failure is (`Rejection(reasons)`) |
  | Adapter | mapping external failures to kinds (`Conflict`, `Unavailable`) |
  | Service | *when* to raise (`OrderRejected`, `OrderNotFound`) |
  | Exception handler | status code + body shape |

- Document error shapes in the route (`responses={422: {"model": ErrorOut}}`) so OpenAPI and clients know them.
- Keep non-trivial formatting (CSV export, report rendering, pagination envelopes) in pure functions; test with fixtures.

---

## Dependency Flow

```
schemas.py / domain types (contracts)
  ↓
domain.py        (pure; imports only stdlib + its own types)
  ↓
ports.py         (Protocols; may import domain types)
  ↓
adapters.py      (implement ports; own ORM models; import domain)
  ↓
service.py       (imports domain + ports; NOT adapters, NOT fastapi)
  ↓
deps.py          (imports adapters + ports; fastapi.Depends)
  ↓
router.py        (imports deps + service + schemas)
  ↓
main.py / errors.py  (includes routers; imports service exceptions)
```

Rules:

- **Domain imports nothing** from the rest of the app, and nothing from `fastapi`/`pydantic`/`sqlalchemy`.
- **`service.py` does not import adapters or `deps`.** It sees Protocols. This is what keeps it testable with fakes and prevents cycles.
- **`schemas.py` may import domain types only for mapping helpers**, never the reverse.
- **Adapters never import services or routers.**
- Exceptions raised by services live in `service.py` (or `exceptions.py`) so `errors.py` can import them without importing routers.
- No cycles. If `orders` needs `customers` and vice versa, extract the shared piece downward (a `shared` domain module or a port).

See [Circular imports](#circular-imports).

---

## Testing Strategy

### Pyramid and tooling

| Test | Target | Needs | Speed |
| ---- | ------ | ----- | ----- |
| **Domain** (write first) | `domain.py` | nothing but `pytest` | ms |
| **Service** | `service.py` | in-memory fakes of ports | ms |
| **Adapter** | repositories/gateways | real DB (SQLite/testcontainers/transactional fixture) or recorded HTTP (`respx`) | 10s of ms |
| **API / entry point** | router + handlers + error mapping | `TestClient`/`httpx.AsyncClient` + `dependency_overrides` | 10s of ms |
| **End-to-end** (few) | critical journeys | real stack / test env | slow |

Assert **outcomes** (returned values, persisted state, status codes), not call sequences.

### Commands

```bash
pytest tests/orders/test_domain.py -q          # domain only: no app import needed
pytest tests/orders -q                         # a feature
pytest -q -k "orders and not api"              # skip HTTP layer
pytest --cov=app.orders.domain --cov-fail-under=100   # optional: hold the domain at full coverage
```

Use the repo's runner if different (`uv run pytest`, `poetry run pytest`, `make test`). Minimal config:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

Dev deps: `pytest`, `httpx` (required by `TestClient`), and `anyio` (ships with FastAPI's deps) or `pytest-asyncio` for async tests.

### Domain tests: parametrized tables

```python
@pytest.mark.parametrize("subtotal, discount", [(0, 0), (9_999, 0), (10_000, 1_000), (25_000, 2_500)])
def test_bulk_discount_boundaries(subtotal, discount):
    assert bulk_discount_cents(subtotal) == discount
```

Cover: happy path, each rule boundary, empty/None/edge input, **every reason** a function can return. Use `ids=[...]` for readable failures.

### Faking adapters

- **Protocol + in-memory class** (preferred): `InMemoryOrderRepository` with the same methods; reuse across service and API tests.
- Inject into services directly in service tests (`place_order(..., orders=repo, now=lambda: FIXED)`).
- Inject into the app with `dependency_overrides`:

  ```python
  @pytest.fixture
  def client():
      app = create_app()  # fresh app per test
      app.dependency_overrides[deps.get_orders] = lambda: repo
      app.dependency_overrides[deps.get_now] = lambda: lambda: FIXED_NOW
      with TestClient(app) as c:  # runs lifespan
          yield c
  ```

  Override the **provider function object** used in `Depends(...)` (the same object, not an alias). Clear overrides (`app.dependency_overrides.clear()`) if you share an app across tests.
- Don't `unittest.mock.patch` deep import paths when an override works.

### Async tests

`TestClient` works for both `def` and `async def` routes. It runs the app in a portal thread.

Use `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")` when the *test itself* must be async (async fixtures, async DB assertions). Mark the test with `@pytest.mark.anyio` and provide `anyio_backend` returning `"asyncio"`, or use `pytest-asyncio`.

`ASGITransport` does **not** run lifespan. If you need lifespan, wrap the app with `asgi-lifespan`'s `LifespanManager`.

### Real DB tests

Run each test in a transaction rolled back at the end (session fixture with `begin()` + `rollback()`), or use SQLite in-memory with `StaticPool` for fast adapter tests. Keep a few against the production engine (testcontainers) for dialect-specific behavior.

---

## Framework Gotchas

### Blocking calls in async endpoints

- `async def` path operations run **on the event loop**. A blocking call inside them (sync SQLAlchemy `Session`, `requests`, `time.sleep`, `open().read()` on big files, CPU-heavy loops) **stalls every request** on that worker.
- `def` path operations (and `def` dependencies) run in a **threadpool**; blocking is fine there (default ~40 threads).
- Pick one coherent mode per feature:
  - Sync stack (sync SQLAlchemy, `requests`): **`def` routes and `def` services**. Simplest; correct.
  - Async stack (`AsyncSession`, `httpx.AsyncClient`): **`async def` all the way**, `await` every I/O.
  - Mixed: call blocking code from `async def` via `await run_in_threadpool(fn, ...)` / `anyio.to_thread.run_sync`; call CPU-heavy work via a process pool/worker.
- Don't declare `async def` "because it's FastAPI". Without an `await`, it only adds risk.
- Domain functions are sync and fast; calling them inline from either mode is fine. If one becomes genuinely CPU-heavy, offload it at the action level, not inside the domain.

### `Depends` caching per request

- Within a single request, a dependency used in several places is **called once** and its result reused (cache key: the callable plus its scopes). This is what lets the router, the repository provider and an auth dependency share one DB session.
- Consequences: (1) don't rely on a dependency being called twice; (2) a provider returning a **factory** (`get_now` returning a callable) is the way to get fresh values each call; (3) pass `Depends(get_x, use_cache=False)` only when you truly need a new instance per use.
- Dependencies are cached **per request, not across requests**. Process-wide singletons need `lru_cache`, `lifespan`/`app.state`, or a module-level instance built once.
- Overrides replace the provider everywhere it is used, which is why every seam must go through one provider function.

### Session and transaction scoping

- **One session per request**, created by a `yield` dependency, closed after the response:

  ```python
  def get_session() -> Iterator[Session]:
      with SessionLocal() as session:
          try:
              yield session
              session.commit()  # unit-of-work boundary: one place
          except Exception:
              session.rollback()
              raise
  ```

- Repositories use the injected session and `add`/`flush`; **they do not commit**. A use-case that spans two repositories is one transaction by construction.
- Do not share a session across requests, threads or background tasks. Background work builds its own session.
- Teardown (code after `yield`) may run after the response has started (behavior varies by FastAPI version), so a commit failure there may not be able to change the status code. If the caller must learn about commit failures, commit explicitly in a unit-of-work step inside the request path, and keep the dependency's job to rollback/close.
- Mind lazy loading: with `AsyncSession`, touching an unloaded relationship outside the session raises. Load what you need inside the repository and translate to domain values before returning (this also fixes ORM leakage). Set `expire_on_commit=False` if returned objects are read after commit.

### ORM models leaking into responses

- Returning an ORM instance works "by accident" through `from_attributes`, then leaks columns, lazy relationships (extra queries, `DetachedInstanceError`), and couples the wire format to the schema.
- Fix structurally: the **repository returns domain dataclasses**; handlers return those; `response_model=OrderOut` filters the output.
- Never accept an ORM model as a request body, and never put ORM models in `Protocol` signatures.
- Mass-assignment: do not `Model(**body.model_dump())` blindly; map explicit fields.

### Pydantic validation vs. domain validation

Covered in [Domain Logic](#pydantic-validation-vs-domain-validation). Short form: schemas check *shape*, the domain checks *policy*, and anything that needs fetched data is domain.

Also, Pydantic v2 coerces by default (`"5"` -> `5`). Use `strict=True` where coercion hides bugs. `model_dump()` returns plain dicts for hand-off. `model_validate` is the only way in.

### Time and uuid injection

- `datetime.now()`/`uuid4()` inside domain or service code makes tests flaky or forces `freezegun`/`patch`.
- Inject **providers** (`now: Callable[[], datetime]`, `new_id: Callable[[], str]`) via `Depends`; the service calls them and passes values to the domain.
- Use timezone-aware UTC (`datetime.now(timezone.utc)`); never naive datetimes.
- DB-generated ids/timestamps (`server_default`) are fine at the adapter level, but if a rule depends on them, generate in the service instead.
- Don't use `Field(default_factory=uuid4)` on *input* schemas for server-set values: the client could supply them.

### Circular imports

Typical cause: `models` <-> `schemas` <-> `services` <-> `routers` importing each other, or `deps.py` importing `main.py` for `app`.

- Follow the [dependency flow](#dependency-flow): lower layers never import higher ones.
- `service.py` imports Protocols, not adapters, so adapters can import domain without a loop.
- Put exceptions in a leaf module; `errors.py` imports them, routers don't import `errors.py`.
- Access the app via `request.app` inside providers, never `from app.main import app`.
- Use `from __future__ import annotations` + `if TYPE_CHECKING:` for type-only imports; avoid function-level imports as a permanent fix (they hide a layering problem).
- Pydantic models referring to each other: define in one module or use string forward refs and `model_rebuild()`.

### Other traps

- **Router-level `Depends` run even if unused** by the handler; use them for auth/cross-cutting only.
- **`response_model` plus returning a Response** bypasses filtering.
- **Sub-dependencies with `yield`**: exit order is reverse of setup; don't rely on it for business logic.
- **Module-level `app = FastAPI(...)` with side effects at import** (DB connections) breaks tests; build in `lifespan`/providers.
- **Settings**: use `pydantic-settings` in `core/config.py`, provided via a cached `get_settings` dependency (overridable); don't read `os.environ` in domain/service.
- **BackgroundTasks** run after the response, in the same process; failures are not visible to the client. Use a real queue for anything that must not be lost.
- **Naming a route function the same as a service function** shadows imports; import the module (`from app.orders import service`) rather than names.

---

## Creating New vs. Migrating

| Aspect | New endpoint | Migrating existing |
| ------ | ------------ | ------------------ |
| Approach | Bottom-up: schemas -> domain -> ports/adapters -> service -> deps -> router | Characterize -> extract -> isolate -> thin |
| Testing | Domain tests first | Pin behavior with `TestClient` tests first |
| Guide | [QUICK_START_NEW_ENDPOINT.md](./QUICK_START_NEW_ENDPOINT.md) | [QUICK_START_MIGRATION.md](./QUICK_START_MIGRATION.md) |

Migration is behavior-preserving and incremental; keep the public URL/response shape stable.

### When to skip a role (FastAPI flavor)

| Feature kind | Skip |
| ------------ | ---- |
| Pure CRUD with no rules | Domain module; service may just call the repository. Still keep schemas separate from ORM models. |
| Health/metrics/static routes | Everything but the router |
| Thin proxy to another API | It *is* a gateway adapter; keep the router thin, contract-test the gateway |
| Single-adapter feature, one implementation forever | A Protocol is optional; inject the concrete class via `Depends` |

The moment a rule appears (a threshold, a state check), add `domain.py` and its test.

---

## Examples

A minimal runnable `orders` feature (place and fetch an order with bulk discount). Full file-by-file walkthrough, including router, wiring and tests, is in [QUICK_START_NEW_ENDPOINT.md](./QUICK_START_NEW_ENDPOINT.md). The essentials:

**Domain (pure) and its table-driven test**

```python
# app/orders/domain.py
BULK_THRESHOLD_CENTS = 10_000
BULK_DISCOUNT_PERCENT = 10


def bulk_discount_cents(subtotal: int) -> int:
    if subtotal < BULK_THRESHOLD_CENTS:
        return 0
    return subtotal * BULK_DISCOUNT_PERCENT // 100
```

```python
# tests/orders/test_domain.py
@pytest.mark.parametrize("subtotal, discount", [(0, 0), (9_999, 0), (10_000, 1_000), (25_000, 2_500)])
def test_bulk_discount_boundaries(subtotal, discount):
    assert bulk_discount_cents(subtotal) == discount
```

**Action** (see [Actions](#actions)), **provider** and **router** (see [Orchestrator](#orchestrator), [Entry Point](#entry-point)), and **API test with overrides** (see [Faking adapters](#faking-adapters)) complete the slice.

### Bad vs. good

```python
# BAD: rule, I/O, clock and transport in one handler
@router.post("/orders")
async def place(body: PlaceOrderIn, db: Session = Depends(get_db)):
    total = sum(l.quantity * db.query(Product).get(l.sku).price for l in body.lines)
    if total > 10_000:
        total = total * 0.9
    order = OrderModel(id=str(uuid4()), total=total, placed_at=datetime.now())
    db.add(order)
    db.commit()
    return order  # ORM model leaks; blocking calls inside async def
```

```python
# GOOD: router parses/delegates; rule is pure; I/O behind ports; values injected
@router.post("", response_model=OrderOut, status_code=201)
def place(body: PlaceOrderIn, orders: OrdersDep, catalog: CatalogDep, now: NowDep, new_id: NewIdDep):
    return service.place_order(
        body.customer_id, body.to_domain(), orders=orders, catalog=catalog, now=now, new_id=new_id
    )
```

---

## Summary

1. **Rules in pure Python** (`domain.py`): no fastapi/pydantic/sqlalchemy, injected time and ids, outcomes as data.
2. **Schemas are boundary contracts**: Pydantic v2, separate in/out, separate from ORM and domain. Shape in schemas, policy in domain.
3. **Services are actions**: keyword-injected ports, "call, call, call", raise application exceptions.
4. **Adapters hide I/O** behind Protocols; ORM models never leave them; the unit of work owns the commit.
5. **`Depends` + `create_app()` + `lifespan` are the composition root**, boring on purpose; `dependency_overrides` is the test seam.
6. **Routers are thin**: parse, delegate, `response_model`; exception handlers map errors.
7. **Test the domain first** with parametrized tables, then services with fakes, then the API with `TestClient`.
8. **Respect the event loop**: `def` for blocking stacks, `async def` only when every I/O is awaited.

Precedence reminder: repo conventions > this set > [generic](../generic/CODE_ORGANIZATION_CONVENTION.md).
