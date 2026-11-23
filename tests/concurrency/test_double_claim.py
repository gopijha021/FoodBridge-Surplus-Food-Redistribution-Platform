"""The headline invariant: one donation, many claimants, exactly one allocation.
Needs a real PostgreSQL (row locks + concurrent transactions):
    TEST_DATABASE_URL=postgresql+psycopg://... pytest tests/concurrency     or     TEST_PGSERVER=1 pytest tests/concurrency
"""
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Allocation, Donation
from app.db.session import SessionLocal, engine
from app.main import app
from tests.helpers import LAT, LON, claim, make_org, make_user, published_donation

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL")

N = 50


def test_50_parallel_claims_exactly_one_wins(client):
    donor_id, donor = make_user("DONOR")
    make_org(donor_id, "DONOR")
    did = published_donation(client, donor)
    ngos = []
    for _ in range(N):
        uid, h = make_user("NGO")
        ngos.append((make_org(uid, "NGO", lat=LAT + 0.01), h))

    def attempt(args):
        org, h = args
        with TestClient(app) as c:
            return claim(c, did, org, h).status_code

    with ThreadPoolExecutor(max_workers=N) as pool:
        codes = list(pool.map(attempt, ngos))

    assert codes.count(201) == 1, codes
    assert codes.count(409) == N - 1, codes
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Allocation).where(Allocation.donation_id == uuid.UUID(did))) == 1
        assert db.get(Donation, uuid.UUID(did)).status == "RESERVED"


def test_parallel_replays_of_same_key_create_one_allocation(client):
    donor_id, donor = make_user("DONOR")
    make_org(donor_id, "DONOR")
    did = published_donation(client, donor)
    uid, h = make_user("NGO")
    org = make_org(uid, "NGO", lat=LAT + 0.01)

    def attempt(_):
        with TestClient(app) as c:
            r = claim(c, did, org, h, key="same-key-12345")
            return r.status_code, r.json().get("id")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))
    assert {code for code, _ in results} <= {200, 201}
    assert len({i for _, i in results}) == 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Allocation)) == 1
