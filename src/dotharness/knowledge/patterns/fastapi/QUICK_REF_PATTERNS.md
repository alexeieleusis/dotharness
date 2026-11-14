# FastAPI Quick Reference

**Cheat sheet. Full rationale: [FASTAPI_ORGANIZATION_CONVENTION.md](./FASTAPI_ORGANIZATION_CONVENTION.md). Generic version: [../generic/QUICK_REF_PATTERNS.md](../generic/QUICK_REF_PATTERNS.md).** Precedence: repo conventions > this set > generic.

---

## Role Map

| Generic role | FastAPI unit | File |
| ------------ | ------------ | ---- |
| Contracts | Pydantic v2 schemas (API) + frozen dataclasses (internal) | `schemas.py`, `domain.py` |
| Domain | Pure functions, outcomes as data | `domain.py` |
| Adapters | Repository/gateway behind a `Protocol` | `ports.py`, `adapters.py` |
| Actions | Service functions, keyword-injected ports | `service.py` |
| Orchestrator | `Depends` providers, `create_app()`, `lifespan` | `deps.py`, `main.py` |
| Entry point | `APIRouter` path operations | `router.py` |
| Presentation | `response_model`, exception handlers | `schemas.py`, `errors.py` |

```
schemas/domain types -> domain -> ports -> adapters -> service -> deps -> router -> main
(service imports Protocols, never adapters or fastapi; domain imports nothing)
```

---

## Templates

**Domain** (pure, with time and ids passed in)

```python
@dataclass(frozen=True)
class Rejection:
    reasons: tuple[str, ...]


def decide_order(*, order_id, customer_id, requested, prices, now) -> Order | Rejection:
    reasons = check_requested(requested)
    if reasons:
        return Rejection(tuple(reasons))
    ...
```

**Domain test** (no app, no fixtures)

```python
@pytest.mark.parametrize("subtotal, discount", [(9_999, 0), (10_000, 1_000)])
def test_bulk_discount(subtotal, discount):
    assert bulk_discount_cents(subtotal) == discount
```

**Port + service**

```python
class OrderRepository(Protocol):
    def add(self, order: Order) -> None: ...

def place_order(customer_id, requested, *, orders, catalog, now, new_id) -> Order:
    outcome = domain.decide_order(order_id=new_id(), ..., prices=catalog.prices_for(...), now=now())
    if isinstance(outcome, domain.Rejection):
        raise OrderRejected(outcome.reasons)
    orders.add(outcome)
    return outcome
```

**Provider + router**

```python
def get_orders(session: SessionDep) -> OrderRepository:
    return SqlOrderRepository(session)
OrdersDep = Annotated[OrderRepository, Depends(get_orders)]

@router.post("", response_model=OrderOut, status_code=201)
def place(body: PlaceOrderIn, orders: OrdersDep, ...):
    return service.place_order(body.customer_id, body.to_domain(), orders=orders, ...)
```

For error mapping and API tests, see [the convention](./FASTAPI_ORGANIZATION_CONVENTION.md#presentation--serialization) and [QUICK_START_NEW_ENDPOINT.md](./QUICK_START_NEW_ENDPOINT.md).

---

## What Goes Where

| Concern | Home |
| ------- | ---- |
| Type/range/format of the payload (`gt=0`, `max_length`, enum) | Pydantic schema |
| Business limits, uniqueness, state transitions, pricing | `domain.py` |
| Anything needing fetched data | `domain.py`, data passed in by the service |
| Authorization rule | `domain.py` (takes the principal). Authentication is a `Depends` |
| Fetch/save, query building, ORM models | adapter |
| Commit/rollback | unit of work (session dependency), not repositories or domain |
| `now()`, `uuid4()`, env, settings | injected providers |
| Status codes, error body shape | exception handlers / `responses=` |
| Choosing implementations | `deps.py` / `create_app()` |
| Process-wide clients (HTTP, engine) | `lifespan` -> `app.state` |
| Follow-up work after response | `BackgroundTasks`/queue entry point calling a service |

---

## Decision Trees

**`def` or `async def`?**

```
Does every I/O in this path have an awaitable client (AsyncSession, httpx.AsyncClient)?
  yes -> async def route + async service (await everything)
  no  -> def route + def service (runs in the threadpool)
Never: blocking call inside async def. Escape hatch: run_in_threadpool.
```

**Schema or domain validation?**

```
Is it about the wire format (type, presence, range, length)? -> schema
Would a product owner recognize the number/rule? -> domain
Does it need a DB/API lookup? -> domain (service fetches, passes in)
```

---

## Good / Bad Checks

| Bad | Good |
| --- | ---- |
| `if total > 10_000` in a route | `bulk_discount_cents()` in `domain.py` with a test |
| `domain.py` imports `fastapi`/`sqlalchemy`/`HTTPException` | domain returns `Rejection`, service raises an application exception, handler maps it |
| `datetime.now()` / `uuid4()` in domain or service | `now`/`new_id` providers via `Depends`. service passes values to domain |
| Route returns an ORM instance | repository returns dataclass and route sets `response_model=OrderOut` |
| One model for request, response and table | `OrderIn`, `OrderOut`, ORM model |
| `service.py` imports `SqlOrderRepository` | `service.py` depends on `OrderRepository` Protocol |
| `session.commit()` scattered in repositories | one explicit `uow.commit()` in the service (not in dependency teardown) |
| `async def` route calling sync SQLAlchemy / `requests` | `def` route, or async driver + `await` |
| Pydantic validator that queries the DB | domain rule with data passed in |
| Reusing the request session in `BackgroundTasks` | task opens its own session. Pass ids |
| `from app.main import app` in providers | `request.app` |
| `HTTPException` raised from services | application exceptions + exception handlers |

---

## Testing Cheat Sheet

```bash
pytest tests/orders/test_domain.py -q    # domain: no app boot
pytest tests/orders -q                   # feature
```

| Test | Fakes |
| ---- | ----- |
| Domain | none (parametrize) |
| Service | in-memory port implementations |
| Adapter | real SQLite/testcontainer, or `respx` for HTTP |
| API | `TestClient` + `dependency_overrides` (override the *provider function*) |

Async tests: `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")`. This client does not run lifespan.

---

## Pre-PR Checklist

- [ ] Every rule is a pure function with a parametrized test
- [ ] `domain.py` has no `fastapi`/`pydantic`/`sqlalchemy` imports
- [ ] Routers and `deps.py` contain no rules. Services read "call, call, call"
- [ ] Separate In/Out schemas. No ORM objects in responses or Protocols
- [ ] `now`/ids injected. One session per request, one commit point
- [ ] `def`/`async def` choice matches the I/O clients used
- [ ] Exception handlers map errors. `responses=` documents them
- [ ] No import cycles. Code follows repo naming and lint
