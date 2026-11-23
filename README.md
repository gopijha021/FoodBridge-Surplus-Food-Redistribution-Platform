# FoodBridge: surplus-food redistribution backend

One time-sensitive donation, many possible claimants, **one** correct allocation, and a fully traceable delivery
lifecycle. Implementation of the *FoodBridge Backend Engineering Project Blueprint* as a modular monolith:
FastAPI + PostgreSQL (PostGIS) + Redis + outbox-driven async workers (Celery over RabbitMQ).

> Prototype. All names, coordinates and sample data are illustrative. Validate food-safety, privacy and local
> operating rules before any live use.

## Quick start

### A. Single process, zero infrastructure (SQLite, embedded worker)
```bash
pip install -e ".[dev]"
python -m scripts.seed                       # demo accounts (password: demo-password-123)
uvicorn app.main:app --reload                # docs at http://localhost:8000/docs
python -m scripts.demo_flow http://localhost:8000   # full publish -> ... -> confirm flow
```
In this mode `EVENT_BACKEND=inline` and `EMBEDDED_WORKER=true`: the API process polls the outbox and runs the
expiry job itself. This matches the blueprint's build order (get the happy path working first).

### B. Full stack (PostGIS, Redis, RabbitMQ, API, worker, beat)
```bash
cd docker && docker compose up --build       # migrations run automatically
docker compose exec api python -m scripts.seed
```

## Demo accounts (after `scripts.seed`)
`admin@example.com`, `donor@example.com`, `ngo1@example.com`, `ngo2@example.com`, `volunteer1@example.com`,
`volunteer2@example.com`, all with password `demo-password-123` (override with `SEED_PASSWORD`).

## Tests
```bash
pytest                          # unit + integration on SQLite (concurrency tests skip)
TEST_PGSERVER=1 pytest          # same, on a real throw-away PostgreSQL (pip package `pgserver`)
TEST_DATABASE_URL=postgresql+psycopg://... pytest tests/concurrency
locust -f tests/load/locustfile.py --host http://localhost:8000 -u 50 -r 10 -t 1m --headless
```
Verified result on PostgreSQL: **26 passed**, including *50 parallel claims on one donation -> exactly one 201, 49 x 409,
exactly one allocation row*, and 20 parallel replays of one idempotency key -> one allocation.

## Layout
```
app/
  main.py                    FastAPI app, correlation IDs, metrics, health
  core/                      config, security (Argon2id, JWT), errors, deps (RBAC), rate limit, logging, locations
  db/                        models, session, outbox helper
  domain/                    enums + state machines (the only way status changes)
  modules/
    auth/ organisations/ donations/ matching/ allocations/ delivery/ impact/ admin/
  workers/                   outbox publisher, consumers, scheduled jobs, celery app
  integrations/notifier.py   email/SMS/push provider stub
migrations/                  Alembic (initial revision creates schema + PostGIS GiST indexes)
scripts/                     seed.py, demo_flow.py     tests/ unit, integration, concurrency, load
docker/docker-compose.yml    Dockerfile at repo root
```

## Blueprint -> code map
| Blueprint requirement | Where |
|---|---|
| Lifecycle DRAFT -> ... -> COMPLETED, terminal states, DISPUTED, REASSIGNMENT_REQUIRED | `domain/state.py` |
| Clients never set status; explicit commands; actor recorded | routers expose `publish / claim / assign / accept / pickup / deliver / confirm-receipt`; `transition()` writes `status_history` |
| Race-safe claim: row lock + unique constraint + idempotency key | `allocations/service.py::claim_donation`, partial unique index `uq_allocations_active_donation`, unique `(claimed_by, idempotency_key)` |
| Eligibility filters, then weighted score (0.45/0.25/0.20/0.10), configurable, logged | `matching/service.py`, weights in `core/config.py` |
| No NGO found: widen radius gradually, alert admin after N minutes | `effective_radius_km` (age-based), `consumers.refresh_matching`, `jobs.reconcile` |
| Volunteer declines/times out without touching NGO allocation | `delivery/service.py::decline`, `jobs.expire_volunteer_offers` |
| Pickup deadline passes: expire/flag, block new claims, notify | `jobs.expire_donations` (every minute, `SKIP LOCKED`), `PickupDeadlineMissed` flag |
| Dispute -> admin review, history preserved | `confirm_receipt` -> DISPUTED; `POST /admin/donations/{id}/resolve-dispute` |
| Transactional outbox, at-least-once, idempotent consumers, DLQ, backoff + jitter | `db/outbox.py`, `workers/outbox.py`, `consumers.dispatch` (`processed_events`), `celery_app.handle_event`, `dead_letter_events` |
| PostGIS radius + GiST index | `matching/geo.py` (`ST_DWithin`; bounding box fallback), `migrations/versions/0001_initial.py` |
| Real-time tracking: latest point in Redis, periodic snapshots, role-based access | `core/locations.py`, `GET /tasks/{id}/stream` (SSE), `/tasks/{id}/location` |
| Security checklist | Argon2id, 15-min JWT + rotating refresh tokens, membership checks (`core/deps.py`), rate limits on login/claim/location, approximate coordinates for non-participants, uniform error shape with correlation ID |
| REST API table | `/api/v1/...` all endpoints from section 7, plus withdraw/cancel/release/decline/fail/history/admin helpers |

## Deliberate deviations (all small)
* **Allocation uniqueness is a *partial* unique index** (`status NOT IN ('RELEASED','CANCELLED')`) instead of a plain
  `UNIQUE(donation_id)`, so a released donation can be re-listed and re-claimed. At most one *live* allocation per
  donation is still guaranteed by the database.
* **Delivery task and impact record are created in the same transaction** as the claim / receipt confirmation (not by a
  consumer) so the core invariants never depend on the broker. Events still drive notifications, assignment and analytics.
* **Volunteer auto-assignment** is an event consumer (`AllocationConfirmed`, `TaskDeclined`) plus the reconcile job;
  NGOs can also assign explicitly, and volunteers can self-accept from `GET /tasks/open`.
* Expiry/deadline checks use the application clock in UTC (single time source per process) rather than `now()` in SQL.
* The Alembic initial revision snapshots the model schema; add later changes with `alembic revision --autogenerate`.
* Quantities are `Float` for the single-recipient MVP. Partial allocation (stretch goal) should move to `NUMERIC` plus a
  `reserved_quantity` invariant.

## Configuration
See `.env.example`. Key switches: `DATABASE_URL`, `USE_POSTGIS`, `REDIS_URL`, `BROKER_URL`,
`EVENT_BACKEND=inline|celery`, `EMBEDDED_WORKER`, `JWT_SECRET` (must be changed outside `APP_ENV=local`),
`DONATION_DEFAULT_RADIUS_KM`, matching weights, timeouts.

## Example
```http
POST /api/v1/donations/{id}/claim
Authorization: Bearer <ngo token>
Idempotency-Key: 5f1c2d3e-...
{"ngo_org_id": "<uuid>"}
```
`201` new allocation | `200` same key replayed (original result) | `409 DONATION_ALREADY_RESERVED` / `DONATION_EXPIRED` |
`403 ORGANISATION_NOT_ELIGIBLE` | `422 NOT_ELIGIBLE` (with reasons) | errors always
`{"error": {"code","message","correlation_id","details"}}`.

## Resume bullet: use only with *your own* measured numbers
Run Locust and note concurrent-claim count, p95 latency at a stated load and coverage, then fill those in. The test
numbers above are the only figures this repo itself can claim.
