# Quick Start: Migrating Existing FastAPI Code

**Use this guide when you refactor a fat route handler, a `crud.py`/god-service, or ORM-model-as-everything code toward the pattern.** It is the FastAPI instantiation of [../generic/QUICK_START_MIGRATION.md](../generic/QUICK_START_MIGRATION.md). Rationale: [FASTAPI_ORGANIZATION_CONVENTION.md](./FASTAPI_ORGANIZATION_CONVENTION.md). Precedence: repo conventions > this document > generic.

**Before you start:** map the roles to the repo's own units ([ADAPTING_TO_A_CODEBASE.md](../generic/ADAPTING_TO_A_CODEBASE.md)). If the repo uses `routers/ services/ crud/ models/ schemas/`, keep those directories. The migration is about *where logic lives*, not folder names.

---

## Key Differences from Creating New

| Aspect | New endpoint | Migrating |
| ------ | ------------ | --------- |
| Approach | Bottom-up | Characterize -> extract -> isolate -> thin |
| Testing | Domain tests first | Pin behavior with `TestClient` tests first |
| Risk | Low | Medium: URL, status codes and response bodies must not change |
| Speed | Faster | Slower: must understand first |

## Principles

- **Behavior-preserving**: a migration PR changes structure, not behavior (status codes, bodies, side effects). Fix bugs separately.
- **Incremental**: the app works after every step.
- **Characterize before you change.**
- **Extract before you rewrite.**
- **Do the domain first.**
- **Keep the public API stable** (paths, schemas, OpenAPI output).
- **Scope tightly**: only the endpoints you are touching.

---

## Typical Smells

```python
@router.post("/orders")
async def place(body: OrderCreate, db: Session = Depends(get_db)):  # sync Session in async def
    if len(body.lines) > 20:
        raise HTTPException(422, "too many lines")  # rule + transport
    total = 0
    for l in body.lines:
        p = db.query(Product).filter_by(sku=l.sku).first()  # I/O mixed with rules
        total += p.price * l.quantity
    if total >= 10_000:
        total = total * 9 // 10  # magic number
    order = Order(id=str(uuid4()), total=total, placed_at=datetime.now())  # hidden id/clock
    db.add(order)
    db.commit()  # commit in handler
    return order  # ORM model leaks
```

---

## Step-by-Step Process

### Step 0: Characterize

1. Read the path operation, its dependencies, and what it calls. Note the inputs, outputs, side effects, and status codes.
2. List the **implicit rules** (conditionals, loops, constants, `HTTPException` branches).
3. Write **characterization tests** at the API level with `TestClient`. Run them against a test DB (SQLite/testcontainers) or with the existing `dependency_overrides`. Pin the *current* responses, including odd ones:

```python
def test_place_order_current_behavior(client):
    r = client.post("/orders", json={"customer_id": "c1", "lines": [{"sku": "widget", "quantity": 4}]})
    assert r.status_code == 200  # even if 201 would be "right"
    body = r.json()
    assert body["total"] == 9_000  # even if the field name is odd
    assert set(body) == {"id", "total", "placed_at", "customer_id"}  # pins leaked columns too
```

Freeze unstable values (`id`, `placed_at`) by overriding providers if they exist, or assert on shape. If clients depend on the OpenAPI output, snapshot `app.openapi()` to prove the contract did not change.

4. Record surprises. Do not fix them yet.

---

### Step 1: Centralize and Separate Contracts

- Give request and response their own Pydantic models (`OrderIn`/`OrderOut`) if one class does both, or if the ORM model is the response.
- Match the **existing wire format exactly** (names, optional fields, status codes).
- Add `response_model=OrderOut`. The pinned test shows whether it changes the output. A leaked column that disappears is a behavior change. Either call the change out, or keep the column in `OrderOut` for now and remove it separately.
- Create frozen dataclasses for the internal shapes you are about to extract.
- Behavior stays unchanged. Check it with the characterization tests and the OpenAPI snapshot.

---

### Step 2: Extract Business Logic into `domain.py`

Look for these in the handler:

- `HTTPException` branches that encode policy
- totals/discounts
- thresholds
- state checks
- string/format conversions
- ownership checks
- branching on `status`/`plan`/`type`

**Before** (inline in the handler):

```python
if len(body.lines) > 20:
    raise HTTPException(422, "too many lines")
...
if total >= 10_000:
    total = total * 9 // 10
```

**After:**

```python
# domain.py: pure, no HTTPException, no ORM, no clock
def check_requested(lines) -> list[str]: ...
def bulk_discount_cents(subtotal: int) -> int: ...
```

Process, one rule at a time:

1. Copy the logic into a pure function. Turn `raise HTTPException` into returned reasons. Turn `datetime.now()`/`uuid4()` into parameters. Replace ORM objects with plain values.
2. Write a **parametrized domain test** covering the cases the old code handled (including the boundary: `9_999` vs `10_000`).
3. Replace the inline logic with a call.
4. Run the characterization tests. Check that the status code and body are the same. Keep the same message text. Map `reasons` to the old `detail` shape for now.

Commit after each extraction if that helps review.

---

### Step 3: Isolate I/O Behind Ports/Adapters

Find `db.query(...)`, `session.execute(...)`, `requests.get(...)`, `open(...)`, `os.environ`, `datetime.now()`, `uuid4()` inside rule-bearing code.

1. Reuse or create a repository/gateway with simple methods (`get`, `add`, `prices_for`). **The repository translates ORM rows to domain dataclasses.**
2. Define a `Protocol` in `ports.py` for each one. Make the service depend on it.
3. Add providers in `deps.py` (`get_orders`, `get_now`, `get_new_id`). Each provider returns the Protocol type.
4. Provide an in-memory fake for tests.

Checks: `domain.py` has no I/O or framework imports. The characterization tests still pass.

Fix session scoping while you are here:

- Use one session per request, from a `yield` dependency.
- Repositories only `add`/`flush`.
- Move the `commit()` out of the handler into the unit of work. **Keep the observable behavior**, that is, when the commit happens relative to the response.
- Do not share the request session with background tasks.

---

### Step 4: Extract the Service (Actions)

Move the remaining sequence (fetch, decide, persist) from the handler into `service.py`:

```python
def place_order(customer_id, requested, *, orders, catalog, now, new_id) -> Order:
    prices = catalog.prices_for(...)
    outcome = domain.decide_order(...)
    if isinstance(outcome, domain.Rejection):
        raise OrderRejected(outcome.reasons)
    orders.add(outcome)
    return outcome
```

- Services take plain values and ports. They do not import `fastapi`, `Request`, or `HTTPException`.
- Replace `HTTPException` with application exceptions. Add handlers in `errors.py` that produce the **same status and body** as before.
- God-service classes: split them by use case. Keep a class only if the repo uses classes with constructor injection.

---

### Step 5: Thin the Router

Reduce the path operation to: **parse -> delegate -> respond.**

```python
# status_code=200 keeps the legacy behavior pinned in Step 0; change to 201 in a separate PR.
# OrderOut must still carry the legacy fields (including leaked columns) pinned in Step 1.
# `def` (vs the smell's `async def`) is the separate scheduling change described below.
@router.post("", response_model=OrderOut, status_code=200)
def place(body: PlaceOrderIn, orders: OrdersDep, catalog: CatalogDep, now: NowDep, new_id: NewIdDep):
    return service.place_order(
        body.customer_id, body.to_domain(), orders=orders, catalog=catalog, now=now, new_id=new_id
    )
```

- No `if` that encodes policy. No direct DB calls.
- Choose `def` or `async def` based on the I/O clients the endpoint uses. The common fix for a sync `Session` inside `async def` is to change it to `def`. That alters scheduling (threadpool) but not responses. Do it as its own step and note it in the PR.
- Preserve the old status code and path.

---

### Step 6: Add Layered Tests

1. Domain tests for every extracted rule (from Step 2).
2. Service tests with in-memory fakes. Rejection persists nothing. Success stores.
3. Adapter tests against a real DB for translation and error mapping.
4. API tests with `dependency_overrides`. Keep characterization tests that express end-to-end behavior. Delete redundant ones.

---

### Step 7: Clean Up

- Remove dead code (`crud.py` helpers, unused schema fields).
- Update docs/comments/OpenAPI descriptions.
- Add an import-linter or ruff `banned-api` rule to keep `domain.py` free of `fastapi`/`pydantic`/`sqlalchemy`.
- Match repo naming.

---

## Migration Checklist

- [ ] Behavior pinned by `TestClient` tests **before** refactoring (and OpenAPI snapshot if clients exist)
- [ ] Each step leaves the app working and shippable
- [ ] Rules are pure, parametrized-tested functions. No `HTTPException` in them
- [ ] I/O behind Protocols/adapters. ORM models do not leak into responses or services
- [ ] One session per request, one commit point. No session reuse in background tasks
- [ ] Router and `deps.py` contain no rules
- [ ] Status codes, paths and bodies unchanged (or the change is called out explicitly)
- [ ] No new circular imports. Layering follows the [dependency flow](./FASTAPI_ORGANIZATION_CONVENTION.md#dependency-flow)
- [ ] No unrelated fixes in the same PR

---

## Common Pitfalls

| Pitfall | Fix |
| ------- | --- |
| Big-bang rewrite | Follow the steps, one endpoint at a time |
| Refactoring without tests | Characterize with `TestClient` first |
| "Fixing" odd status codes mid-extraction | Record, extract faithfully, fix separately |
| Extracting a function that still takes a `Session` or `Request` | Pass plain values and push I/O to an adapter |
| Adding `response_model` and silently dropping fields clients use | Pin the body first, then change deliberately |
| Moving `HTTPException` into the domain | Return reasons, then map them in a handler |
| Changing `async def` to `def` and calling it "refactoring" | Separate, labeled change |
| Introducing Protocols/layers for pure CRUD | Skip roles that earn nothing. Keep the schema separate from the ORM model |
| Overriding the wrong callable in tests | Override the exact function object in `Depends(...)` |
| Migrating neighboring endpoints "while you're there" | Limit scope |

---

## When to Stop

Stop when rules are pure and tested, I/O sits behind adapters, and the router and `deps.py` are boring. A partial migration that delivers testable domain logic is a success.

See [QUICK_REF_PATTERNS.md](./QUICK_REF_PATTERNS.md) for templates and [FASTAPI_ORGANIZATION_CONVENTION.md](./FASTAPI_ORGANIZATION_CONVENTION.md) for the rationale.
