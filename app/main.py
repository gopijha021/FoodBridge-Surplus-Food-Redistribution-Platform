import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from sqlalchemy import text

from app.core.config import settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, correlation_id_var
from app.db.session import SessionLocal
from app.modules.admin.router import router as admin_router
from app.modules.allocations.router import router as allocations_router
from app.modules.auth.router import router as auth_router
from app.modules.delivery.router import router as delivery_router
from app.modules.donations.router import router as donations_router
from app.modules.impact.router import router as impact_router
from app.modules.organisations.router import router as organisations_router

configure_logging()
log = logging.getLogger("foodbridge")

REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("http_request_seconds", "HTTP latency", ["method", "path"])


async def _embedded_worker() -> None:
    """Local single-process mode (EMBEDDED_WORKER=true, EVENT_BACKEND=inline): no broker needed."""
    from app.workers.jobs import expire_donations, reconcile
    from app.workers.outbox import publish_pending

    def tick(n: int) -> None:
        with SessionLocal() as db:
            publish_pending(db)
            if n % 30 == 0:
                expire_donations(db)
            if n % 150 == 0:
                reconcile(db)

    n = 0
    while True:
        try:
            await asyncio.get_running_loop().run_in_executor(None, tick, n)
        except Exception:
            log.exception("embedded worker tick failed")
        n += 1
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if settings.embedded_worker and settings.event_backend == "inline":
        task = asyncio.create_task(_embedded_worker())
    yield
    if task:
        task.cancel()


app = FastAPI(title="FoodBridge API", version="1.0.0", lifespan=lifespan)
register_error_handlers(app)


@app.middleware("http")
async def correlation_and_metrics(request: Request, call_next):
    cid = request.headers.get("x-request-id") or f"req_{uuid.uuid4().hex}"
    request.state.correlation_id = cid
    token = correlation_id_var.set(cid)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    finally:
        correlation_id_var.reset(token)
    elapsed = time.perf_counter() - start
    route = request.scope.get("route")
    path = route.path if route else request.url.path  # templated path keeps metric cardinality bounded
    REQUESTS.labels(request.method, path, response.status_code).inc()
    LATENCY.labels(request.method, path).observe(elapsed)
    response.headers["X-Request-ID"] = cid
    log.info("request", extra={"extra_fields": {"method": request.method, "path": request.url.path,
                                                 "status": response.status_code, "ms": round(elapsed * 1000, 1)}})
    return response


@app.get("/health", tags=["ops"])
def health():
    return {"status": "ok"}


@app.get("/health/ready", tags=["ops"])
def ready(response: Response):
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        response.status_code = 503
        return {"status": "database_unavailable"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


for r in (auth_router, organisations_router, impact_router, donations_router, allocations_router, delivery_router, admin_router):
    app.include_router(r, prefix="/api/v1")
