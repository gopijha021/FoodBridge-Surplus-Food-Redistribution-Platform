import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.db.models import DeadLetterEvent, DeliveryTask, Notification, OutboxEvent, ProcessedEvent
from app.db.session import SessionLocal
from app.workers import outbox
from app.workers.consumers import dispatch, record_dead_letter
from app.workers.jobs import reconcile
from tests.helpers import LAT, LON, claim, drain_events, make_org, make_user, published_donation


def _setup(client):
    donor_id, donor = make_user("DONOR")
    make_org(donor_id, "DONOR")
    ngo_id, ngo = make_user("NGO")
    ngo_org = make_org(ngo_id, "NGO", lat=LAT + 0.01, lon=LON)
    return donor, ngo, ngo_org


def test_outbox_survives_broker_outage(client, monkeypatch):
    """Failure injection: broker down after DB commit -> event stays unpublished -> published later."""
    donor, *_ = _setup(client)
    published_donation(client, donor)

    def boom(ev):
        raise ConnectionError("broker unavailable")

    monkeypatch.setattr(outbox, "publish_event", boom)
    with SessionLocal() as db:
        assert outbox.publish_pending(db) == 0
        row = db.scalar(select(OutboxEvent).where(OutboxEvent.event_type == "DonationPublished"))
        assert row.published_at is None and row.attempts == 1
    monkeypatch.undo()
    assert drain_events() >= 1
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(OutboxEvent).where(OutboxEvent.published_at.is_(None))) == 0


def test_consumers_are_idempotent_on_redelivery(client):
    donor, ngo, ngo_org = _setup(client)
    published_donation(client, donor)
    with SessionLocal() as db:
        ev = db.scalar(select(OutboxEvent).where(OutboxEvent.event_type == "DonationPublished"))
        evd = outbox.event_to_dict(ev)
    with SessionLocal() as db:
        dispatch(db, evd)
        n1 = db.scalar(select(func.count()).select_from(Notification))
        dispatch(db, evd)  # at-least-once delivery: same event again
        dispatch(db, evd)
        assert db.scalar(select(func.count()).select_from(Notification)) == n1 > 0
        assert db.scalar(select(func.count()).select_from(ProcessedEvent)) == 4  # one row per consumer


def test_dead_letter_recording(client):
    ev = {"id": str(uuid.uuid4()), "event_type": "Broken", "payload": {}}
    with SessionLocal() as db:
        record_dead_letter(db, ev, "ValueError('x')")
        assert db.scalar(select(func.count()).select_from(DeadLetterEvent)) == 1


def test_offer_timeout_returns_task_to_pool(client):
    donor, ngo, ngo_org = _setup(client)
    vol_id, vol = make_user("VOLUNTEER")
    did = published_donation(client, donor)
    r = claim(client, did, ngo_org, ngo).json()
    client.post(f"/api/v1/allocations/{r['id']}/assign", json={"volunteer_id": str(vol_id)}, headers=ngo)
    later = datetime.now(timezone.utc) + timedelta(minutes=30)
    with SessionLocal() as db:
        stats = reconcile(db, now=later)
        assert stats["offers_timed_out"] == 1
        t = db.scalar(select(DeliveryTask))
        assert t.status == "OPEN" and t.volunteer_id is None and str(vol_id) in t.declined_volunteer_ids
    assert client.get(f"/api/v1/allocations/{r['id']}", headers=ngo).json()["status"] == "CONFIRMED"


def test_reconcile_flags_missed_pickup_and_overdue_receipt_once(client):
    donor, ngo, ngo_org = _setup(client)
    did = published_donation(client, donor)
    claim(client, did, ngo_org, ngo)
    later = datetime.now(timezone.utc) + timedelta(hours=3)  # after pickup_end (2h)
    with SessionLocal() as db:
        assert reconcile(db, now=later)["pickup_missed"] == 1
        assert reconcile(db, now=later)["pickup_missed"] == 0  # flagged only once


def test_health_metrics_and_correlation_id(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/health/ready").json() == {"status": "ready"}
    r = client.get("/api/v1/auth/me", headers={"X-Request-ID": "req_abc"})
    assert r.status_code == 401 and r.json()["error"]["correlation_id"] == "req_abc" and r.headers["x-request-id"] == "req_abc"
    assert "http_requests_total" in client.get("/metrics").text
