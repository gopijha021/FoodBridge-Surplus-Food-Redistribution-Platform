import os
import tempfile

# --- configure the app BEFORE it is imported -----------------------------------------------------------------
_url = os.environ.get("TEST_DATABASE_URL")
if not _url and os.environ.get("TEST_PGSERVER"):  # real PostgreSQL via the `pgserver` pip package
    import pgserver

    _srv = pgserver.get_server(tempfile.mkdtemp(prefix="fb_pg_"))
    _url = _srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
if not _url:
    _url = f"sqlite:///{tempfile.mkdtemp(prefix='fb_')}/test.db"
os.environ.update(DATABASE_URL=_url, EVENT_BACKEND="inline", EMBEDDED_WORKER="false", RATE_LIMIT_ENABLED="false",
                  JWT_SECRET="test-secret", USE_POSTGIS="false", REDIS_URL="", APP_ENV="local")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s
