# Quick Start: Creating a New FastAPI Endpoint/Feature

**Use this guide to add a feature to a FastAPI service from scratch.** It is the FastAPI version of [../generic/QUICK_START_NEW_FEATURE.md](../generic/QUICK_START_NEW_FEATURE.md). For the rationale, see [FASTAPI_ORGANIZATION_CONVENTION.md](./FASTAPI_ORGANIZATION_CONVENTION.md). Precedence: repo conventions > this guide > the generic guide.

**Before you start:** look at a neighboring healthy feature in the repo. Match its layout, naming, and dependency-injection style. The file names below are the defaults. Map them onto local equivalents ([ADAPTING_TO_A_CODEBASE.md](../generic/ADAPTING_TO_A_CODEBASE.md)).

The example is a small `orders` feature. `POST /orders` prices and places an order (10% bulk discount at or above 10,000 cents). `GET /orders/{id}` fetches it. **All code below was run: 15 tests pass** on Python 3.12, FastAPI 0.162, Pydantic 2.14, pytest.

---

## What You'll Create

```
app/
├── main.py                 # create_app(), lifespan (edit once, then rarely)
├── errors.py               # exception handlers (add yours)
└── orders/
    ├── schemas.py          # Pydantic v2 in/out contracts
    ├── domain.py           # PURE rules              <- tests first
    ├── ports.py            # Protocols
    ├── adapters.py         # implementations (reuse existing ones if any)
    ├── service.py          # actions
    ├── deps.py             # Depends providers (composition root)
    └── router.py           # thin entry point
tests/orders/
    ├── test_domain.py      # HIGHEST PRIORITY
    ├── test_service.py
    └── test_api.py
```

Skip each role the feature does not need ([When to skip a role](./FASTAPI_ORGANIZATION_CONVENTION.md#when-to-skip-a-role-fastapi-flavor)).

---

## Step-by-Step Process

### 0. Clarify the Behavior

- **Trigger**: HTTP route(s), method, auth
- **Inputs & outputs**: fields, which are server-set
- **Rules**: every "if/when/unless" (limits, uniqueness, thresholds)
- **Side effects**: what is stored/sent
- **Failure modes**: each failure and the status code/body the client should see
- **Sync or async stack?** Does the repo use `AsyncSession`/`httpx.AsyncClient` or sync equivalents? That decides `def` vs. `async def` ([Gotchas](./FASTAPI_ORGANIZATION_CONVENTION.md#blocking-calls-in-async-endpoints)).

---

### 1. Define Contracts First

Put domain dataclasses at the top of `domain.py` (shown in Step 2). Put API schemas in `schemas.py`. Schemas validate shape only. Use separate In and Out models. Set `from_attributes=True` on outputs.

```python
# app/orders/schemas.py
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.orders import domain


class LineIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    quantity: int = Field(gt=0)


class PlaceOrderIn(BaseModel):
    customer_id: str = Field(min_length=1)
    lines: list[LineIn]

    def to_domain(self) -> list[domain.RequestedLine]:
        return [domain.RequestedLine(l.sku, l.quantity) for l in self.lines]


class LineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sku: str
    quantity: int
    unit_price_cents: int


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    customer_id: str
    lines: list[LineOut]
    discount_cents: int
    total_cents: int
    placed_at: datetime


class ErrorOut(BaseModel):
    detail: str
    reasons: list[str] = []
```

If the repo generates clients from OpenAPI, regenerate after changing schemas.

---

### 2. Write the Domain (Pure)

Do not use `fastapi`, `pydantic`, `sqlalchemy`, a clock, or uuid here. Outcomes are data. Pass `now` and `order_id` in.

```python
# app/orders/domain.py
"""Pure order rules. No fastapi, pydantic, sqlalchemy, clock, or uuid imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Sequence

MAX_LINES = 20
BULK_THRESHOLD_CENTS = 10_000
BULK_DISCOUNT_PERCENT = 10


@dataclass(frozen=True)
class RequestedLine:
    sku: str
    quantity: int


@dataclass(frozen=True)
class Line:
    sku: str
    quantity: int
    unit_price_cents: int


@dataclass(frozen=True)
class Order:
    id: str
    customer_id: str
    lines: tuple[Line, ...]
    discount_cents: int
    total_cents: int
    placed_at: datetime


@dataclass(frozen=True)
class Rejection:
    reasons: tuple[str, ...]


def check_requested(lines: Sequence[RequestedLine]) -> list[str]:
    """Business validation (Pydantic only checks shape, not policy)."""
    reasons: list[str] = []
    if not lines:
        reasons.append("order must have at least one line")
    if len(lines) > MAX_LINES:
        reasons.append(f"order may have at most {MAX_LINES} lines")
    skus = [line.sku for line in lines]
    if len(set(skus)) != len(skus):
        reasons.append("duplicate sku")
    return reasons


def price_lines(lines: Sequence[RequestedLine], prices: Mapping[str, int]) -> tuple[tuple[Line, ...], list[str]]:
    missing = [f"unknown sku: {l.sku}" for l in lines if l.sku not in prices]
    priced = tuple(Line(l.sku, l.quantity, prices[l.sku]) for l in lines if l.sku in prices)
    return priced, missing


def subtotal_cents(lines: Sequence[Line]) -> int:
    return sum(l.quantity * l.unit_price_cents for l in lines)


def bulk_discount_cents(subtotal: int) -> int:
    if subtotal < BULK_THRESHOLD_CENTS:
        return 0
    return subtotal * BULK_DISCOUNT_PERCENT // 100


def decide_order(
    *,
    order_id: str,
    customer_id: str,
    requested: Sequence[RequestedLine],
    prices: Mapping[str, int],
    now: datetime,
) -> Order | Rejection:
    """Pure decision: id and time are passed in, outcome is data."""
    reasons = check_requested(requested)
    if reasons:
        return Rejection(tuple(reasons))
    lines, missing = price_lines(requested, prices)
    if missing:
        return Rejection(tuple(missing))
    subtotal = subtotal_cents(lines)
    discount = bulk_discount_cents(subtotal)
    return Order(order_id, customer_id, lines, discount, subtotal - discount, now)
```

Note what is *not* here: `quantity > 0` (wire shape, in the schema) versus "at most 20 lines / no duplicates / known SKU" (policy, here).

---

### 3. Test the Domain Immediately

Use parametrized test tables. Do not use fixtures. Do not import the app.

```python
# tests/orders/test_domain.py
from datetime import datetime, timezone

import pytest

from app.orders.domain import (
    Order,
    RequestedLine as R,
    Rejection,
    bulk_discount_cents,
    check_requested,
    decide_order,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PRICES = {"widget": 2_500, "gadget": 999}


@pytest.mark.parametrize(
    "lines, expected",
    [
        ([], ["order must have at least one line"]),
        ([R("a", 1), R("a", 2)], ["duplicate sku"]),
        ([R(f"s{i}", 1) for i in range(21)], ["order may have at most 20 lines"]),
        ([R("a", 1)], []),
    ],
    ids=["empty", "duplicate", "too-many", "ok"],
)
def test_check_requested(lines, expected):
    assert check_requested(lines) == expected


@pytest.mark.parametrize(
    "subtotal, discount",
    [(0, 0), (9_999, 0), (10_000, 1_000), (25_000, 2_500)],
)
def test_bulk_discount_boundaries(subtotal, discount):
    assert bulk_discount_cents(subtotal) == discount


def decide(requested):
    return decide_order(order_id="o1", customer_id="c1", requested=requested, prices=PRICES, now=NOW)


def test_decide_prices_and_discounts():
    result = decide([R("widget", 4), R("gadget", 1)])  # 10_000 + 999
    assert isinstance(result, Order)
    assert (result.discount_cents, result.total_cents) == (1_099, 9_900)
    assert result.placed_at == NOW


def test_decide_rejects_unknown_sku():
    assert decide([R("nope", 1)]) == Rejection(("unknown sku: nope",))
```

```bash
pytest tests/orders/test_domain.py -q
```

Cover: happy path, each rule boundary, empty/edge input, every reason a function can return.

---

### 4. Ports and Adapters

Search for an existing repository or gateway first. Define the seam as a `Protocol`. Implement it in the adapter. Translate external shapes at this boundary.

```python
# app/orders/ports.py
from typing import Mapping, Protocol, Sequence

from app.orders.domain import Order


class OrderRepository(Protocol):
    def add(self, order: Order) -> None: ...
    def get(self, order_id: str) -> Order | None: ...


class PriceCatalog(Protocol):
    def prices_for(self, skus: Sequence[str]) -> Mapping[str, int]: ...
```

```python
# app/orders/adapters.py  (in-memory; doubles as the test fake)
from typing import Mapping, Sequence

from app.orders.domain import Order


class InMemoryOrderRepository:
    def __init__(self) -> None:
        self._rows: dict[str, Order] = {}

    def add(self, order: Order) -> None:
        self._rows[order.id] = order

    def get(self, order_id: str) -> Order | None:
        return self._rows.get(order_id)


class StaticPriceCatalog:
    def __init__(self, prices: Mapping[str, int]) -> None:
        self._prices = dict(prices)

    def prices_for(self, skus: Sequence[str]) -> Mapping[str, int]:
        return {s: self._prices[s] for s in skus if s in self._prices}
```

A real repository follows the same interface:

```python
class SqlOrderRepository:  # implements OrderRepository
    def __init__(self, session: Session) -> None:
        self._s = session

    def add(self, order: Order) -> None:
        self._s.add(OrderRow.from_domain(order))
        try:
            self._s.flush()  # surface constraint violations now; do NOT commit here
        except IntegrityError as exc:
            self._s.rollback()
            raise Conflict(str(exc.orig)) from exc

    def get(self, order_id: str) -> Order | None:
        row = self._s.get(OrderRow, order_id)
        return row.to_domain() if row else None  # ORM row never leaves the adapter
```

Map external failures (`IntegrityError` -> `Conflict`) here. `add` flushes so a duplicate key surfaces inside the request path, but the flush does not make the write durable.

**Do not commit in the `yield` dependency's teardown.** Code after `yield` may run after the response has started. A commit failure there (duplicate key, deadlock) cannot change the status code: the client could get a `201` for an order that was never persisted. Instead, the service calls an explicit unit-of-work `commit()` as its last step. That call maps commit-time `IntegrityError` to `Conflict` before the response is built. The dependency only rolls back on error and closes. See [Session scoping](./FASTAPI_ORGANIZATION_CONVENTION.md#session-and-transaction-scoping) for the `SqlUnitOfWork` adapter. (The in-memory demo below has no transaction, so it omits the unit of work.)

---

### 5. State (If Needed)

Usually the feature needs no state. Keep the request-scoped session in a `yield` dependency. Create process-wide clients in `lifespan` and store them on `app.state`. See [State](./FASTAPI_ORGANIZATION_CONVENTION.md#state).

---

### 6. Build the Action (Service)

"Call, call, call". Collaborators are keyword-only. Raise application exceptions, never `HTTPException`.

```python
# app/orders/service.py
"""Actions: call, call, call. No business branching beyond 'did the domain accept?'."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Sequence

from app.orders import domain
from app.orders.ports import OrderRepository, PriceCatalog


class OrderRejected(Exception):
    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = list(reasons)


class OrderNotFound(Exception):
    pass


def place_order(
    customer_id: str,
    requested: Sequence[domain.RequestedLine],
    *,
    orders: OrderRepository,
    catalog: PriceCatalog,
    now: Callable[[], datetime],
    new_id: Callable[[], str],
) -> domain.Order:
    prices = catalog.prices_for([l.sku for l in requested])
    outcome = domain.decide_order(
        order_id=new_id(),
        customer_id=customer_id,
        requested=requested,
        prices=prices,
        now=now(),
    )
    if isinstance(outcome, domain.Rejection):
        raise OrderRejected(outcome.reasons)
    orders.add(outcome)
    return outcome


def get_order(order_id: str, *, orders: OrderRepository) -> domain.Order:
    order = orders.get(order_id)
    if order is None:
        raise OrderNotFound(order_id)
    return order
```

---

### 7. Wire Providers, Router, App

**Providers** (keep them plain. They return Protocols and are the test seam):

```python
# app/orders/deps.py
"""Composition root for the orders feature: the only place that picks implementations."""

from datetime import datetime, timezone
from functools import lru_cache
from typing import Annotated, Callable
from uuid import uuid4

from fastapi import Depends

from app.orders.adapters import InMemoryOrderRepository, StaticPriceCatalog
from app.orders.ports import OrderRepository, PriceCatalog


@lru_cache
def _repo() -> InMemoryOrderRepository:  # process-wide state; swap for a DB session dep
    return InMemoryOrderRepository()


def get_orders() -> OrderRepository:
    return _repo()


def get_catalog() -> PriceCatalog:
    return StaticPriceCatalog({"widget": 2_500, "gadget": 999})


def get_now() -> Callable[[], datetime]:
    return lambda: datetime.now(timezone.utc)


def get_new_id() -> Callable[[], str]:
    return lambda: str(uuid4())


OrdersDep = Annotated[OrderRepository, Depends(get_orders)]
CatalogDep = Annotated[PriceCatalog, Depends(get_catalog)]
NowDep = Annotated[Callable[[], datetime], Depends(get_now)]
NewIdDep = Annotated[Callable[[], str], Depends(get_new_id)]
```

In a real service, `get_orders` takes a session dependency and returns `SqlOrderRepository(session)`. The `lru_cache` singleton here only keeps the demo store alive across requests.

**Router** (parse -> delegate -> respond):

```python
# app/orders/router.py
"""Entry point: parse -> delegate -> respond."""

from fastapi import APIRouter, status

from app.orders import service
from app.orders.deps import CatalogDep, NewIdDep, NowDep, OrdersDep
from app.orders.schemas import ErrorOut, OrderOut, PlaceOrderIn

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "",
    response_model=OrderOut,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorOut}},  # policy rejection; 422 stays FastAPI's HTTPValidationError
)
def place_order(body: PlaceOrderIn, orders: OrdersDep, catalog: CatalogDep, now: NowDep, new_id: NewIdDep):
    return service.place_order(
        body.customer_id,
        body.to_domain(),
        orders=orders,
        catalog=catalog,
        now=now,
        new_id=new_id,
    )


@router.get("/{order_id}", response_model=OrderOut, responses={404: {"model": ErrorOut}})
def get_order(order_id: str, orders: OrdersDep):
    return service.get_order(order_id, orders=orders)
```

**Error mapping and app factory:**

```python
# app/errors.py
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.orders.service import OrderNotFound, OrderRejected


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(OrderRejected)
    async def _rejected(_: Request, exc: OrderRejected) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": "order rejected", "reasons": exc.reasons})

    @app.exception_handler(OrderNotFound)
    async def _missing(_: Request, exc: OrderNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "order not found"})
```

```python
# app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.errors import register_error_handlers
from app.orders.router import router as orders_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # open long-lived resources (engine, http client) here; store on app.state
    yield
    # close them here


def create_app() -> FastAPI:
    app = FastAPI(lifespan=lifespan)
    app.include_router(orders_router)
    register_error_handlers(app)
    return app


app = create_app()
```

`def` routes run in the threadpool, which is correct for blocking adapters. With async adapters, make the route and service `async def`. Await the port calls.

---

### 8. Write the Remaining Tests

| Test | What | Fakes |
| ---- | ---- | ----- |
| Service | Rejection persists nothing. Success stores the order | In-memory ports |
| Adapter | ORM <-> domain translation, error mapping | Real DB (SQLite/testcontainers) |
| API | Status codes, body shape, error mapping | `dependency_overrides` |

```python
# tests/orders/test_service.py
from datetime import datetime, timezone

import pytest

from app.orders.adapters import InMemoryOrderRepository, StaticPriceCatalog
from app.orders.domain import RequestedLine
from app.orders.service import OrderRejected, place_order


def test_rejection_persists_nothing():
    repo = InMemoryOrderRepository()
    with pytest.raises(OrderRejected):
        place_order(
            "c1",
            [],
            orders=repo,
            catalog=StaticPriceCatalog({}),
            now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc),
            new_id=lambda: "x",
        )
    assert repo.get("x") is None
```

```python
# tests/orders/test_api.py
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.orders import deps
from app.orders.adapters import InMemoryOrderRepository, StaticPriceCatalog


@pytest.fixture
def client():
    app = create_app()
    repo = InMemoryOrderRepository()
    app.dependency_overrides[deps.get_orders] = lambda: repo
    app.dependency_overrides[deps.get_catalog] = lambda: StaticPriceCatalog({"widget": 2_500})
    app.dependency_overrides[deps.get_now] = lambda: lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
    app.dependency_overrides[deps.get_new_id] = lambda: lambda: "order-1"
    with TestClient(app) as c:
        yield c


def test_place_then_get(client):
    r = client.post("/orders", json={"customer_id": "c1", "lines": [{"sku": "widget", "quantity": 2}]})
    assert r.status_code == 201
    assert r.json()["id"] == "order-1" and r.json()["total_cents"] == 5_000
    assert client.get("/orders/order-1").json() == r.json()


def test_shape_error_is_422_from_pydantic(client):
    r = client.post("/orders", json={"customer_id": "c1", "lines": [{"sku": "widget", "quantity": 0}]})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # HTTPValidationError shape: detail is a list


def test_policy_rejection_maps_to_409_with_reasons(client):
    r = client.post("/orders", json={"customer_id": "c1", "lines": []})
    assert r.status_code == 409
    assert r.json() == {"detail": "order rejected", "reasons": ["order must have at least one line"]}  # ErrorOut shape


def test_missing_is_404(client):
    assert client.get("/orders/zzz").status_code == 404
```

Run everything:

```bash
pip install fastapi httpx pytest        # or: uv add --dev httpx pytest
pytest -q                               # needs pythonpath = ["."] in pyproject, or run from repo root with a conftest
```

Async tests: `httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")` with `@pytest.mark.anyio` (verified). To test only one backend, add an `anyio_backend` fixture that returns `"asyncio"`.

---

## Checklist Before Opening a PR

- [ ] Every business rule is a pure function with a parametrized test
- [ ] `grep -nE "^(from|import) (fastapi|pydantic|sqlalchemy)" app/<feature>/domain.py` is empty
- [ ] Router and `deps.py` contain no rules. The service reads "call, call, call"
- [ ] Separate In/Out schemas
- [ ] No ORM objects in responses, ports, or domain
- [ ] `now`/ids injected via `Depends`
- [ ] One session per request
- [ ] One explicit commit point in the request path (not in dependency teardown)
- [ ] `def` vs. `async def` matches the I/O clients used
- [ ] No blocking in `async def`
- [ ] Errors mapped by exception handlers
- [ ] Errors documented with `responses=`
- [ ] Domain tests run without importing the app
- [ ] Follows repo naming/lint
- [ ] OpenAPI clients regenerated if applicable

---

## Common Pitfalls

| Pitfall | Fix |
| ------- | --- |
| Starting with the router | Start with schemas and domain. The router is last |
| Pydantic validator hitting the DB, or holding a business limit | Move to a domain function. The service passes the data in |
| `datetime.now()`/`uuid4()` in rules | Inject providers. Pass values to the domain |
| Returning an ORM instance | The repository returns dataclasses. Set `response_model` |
| `service.py` importing the concrete repository | Import the Protocol. Wire in `deps.py` |
| Sync DB calls in `async def` route | `def` route (threadpool) or an async driver |
| Overriding the wrong callable in tests | Override the exact provider function used in `Depends` |
| `HTTPException` from services/domain | Raise application exceptions. Map them in `errors.py` |
| Duplicating an existing repository | Search and reuse |

See [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md) for templates and [FASTAPI_ORGANIZATION_CONVENTION.md](./FASTAPI_ORGANIZATION_CONVENTION.md) for the rationale.
