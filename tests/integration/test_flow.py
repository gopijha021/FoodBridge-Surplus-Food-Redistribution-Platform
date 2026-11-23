import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import Allocation, Donation, ImpactRecord, Notification, StatusHistory
from app.db.session import SessionLocal
from tests.helpers import (
    LAT, LON, auth, claim, donation_payload, drain_events, make_org, make_user, published_donation,
)

V1 = "/api/v1"


def setup_actors(client, **ngo_kw):
    donor_id, donor = make_user("DONOR")
    make_org(donor_id, "DONOR")
    ngo_id, ngo = make_user("NGO")
    ngo_org = make_org(ngo_id, "NGO", lat=LAT + 0.02, lon=LON + 0.02, **ngo_kw)
    vol_id, vol = make_user("VOLUNTEER")
    return donor, ngo, ngo_org, vol_id, vol


def test_register_login_refresh_rotation(client):
    r = client.post(f"{V1}/auth/register", json={"name": "A", "email": "a@example.com", "password": "password123", "role": "DONOR"})
    assert r.status_code == 201
    assert client.post(f"{V1}/auth/register", json={"name": "A", "email": "A@example.com", "password": "password123", "role": "DONOR"}).status_code == 409
    assert client.post(f"{V1}/auth/login", json={"email": "a@example.com", "password": "wrong"}).status_code == 401
    tok = client.post(f"{V1}/auth/login", json={"email": "a@example.com", "password": "password123"}).json()
    assert client.get(f"{V1}/auth/me", headers=auth(tok["access_token"])).json()["role"] == "DONOR"
    new = client.post(f"{V1}/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert new.status_code == 200
    replay = client.post(f"{V1}/auth/refresh", json={"refresh_token": tok["refresh_token"]})
    assert replay.status_code == 401 and replay.json()["error"]["code"] == "INVALID_REFRESH_TOKEN"
    assert client.post(f"{V1}/auth/register", json={"name": "x", "email": "x@example.com", "password": "password123", "role": "ADMIN"}).status_code == 422


def test_end_to_end_happy_path(client):
    donor, ngo, ngo_org, vol_id, vol = setup_actors(client)
    assert client.post(f"{V1}/volunteers/me/location", json={"latitude": LAT, "longitude": LON}, headers=vol).status_code == 204
    did = published_donation(client, donor)
    drain_events()  # DonationPublished -> matching -> notify NGO

    near = client.get(f"{V1}/donations/nearby", params={"ngo_org_id": str(ngo_org)}, headers=ngo).json()["items"]
    assert [i["donation"]["id"] for i in near] == [did]
    assert near[0]["donation"]["location"]["precision"] == "approximate"  # privacy until allocated
    assert 0 < near[0]["match"]["score"] <= 1

    r = claim(client, did, ngo_org, ngo)
    assert r.status_code == 201 and r.json()["status"] == "CONFIRMED" and r.json()["donation_status"] == "RESERVED"
    alloc_id, task_id = r.json()["id"], r.json()["task_id"]
    exact = client.get(f"{V1}/donations/{did}", headers=ngo).json()
    assert exact["location"]["precision"] == "exact"

    drain_events()  # AllocationConfirmed -> auto-offer to nearest volunteer
    t = client.get(f"{V1}/tasks/{task_id}", headers=vol).json()
    assert t["status"] == "OFFERED" and t["volunteer_id"] == str(vol_id)

    assert client.post(f"{V1}/tasks/{task_id}/accept", headers=vol).json()["status"] == "ACCEPTED"
    assert client.post(f"{V1}/tasks/{task_id}/pickup", headers=vol).json()["status"] == "PICKED_UP"
    assert client.post(f"{V1}/tasks/{task_id}/deliver", headers=vol).json()["status"] == "DELIVERED"
    drain_events()

    r = client.post(f"{V1}/allocations/{alloc_id}/confirm-receipt", json={"received_quantity": 80, "condition_ok": True}, headers=ngo)
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED" and r.json()["donation_status"] == "COMPLETED"
    drain_events()

    with SessionLocal() as db:
        rec = db.scalar(select(ImpactRecord))
        assert rec.quantity_redistributed == 80 and str(rec.donation_id) == did
        assert db.scalar(select(func.count()).select_from(Notification)) > 0
        hist = db.scalars(select(StatusHistory)).all()
        assert all(h.actor_id is not None or h.reason for h in hist)  # every transition has an actor (or a system reason)

    h = client.get(f"{V1}/donations/{did}/history", headers=donor).json()["items"]
    assert [x["to"] for x in h if x["entity_type"] == "DONATION"] == [
        "DRAFT", "PUBLISHED", "RESERVED", "ASSIGNED", "PICKED_UP", "DELIVERED", "COMPLETED"]
    org_id = client.get(f"{V1}/organisations", headers=donor).json()[0]["id"]
    imp = client.get(f"{V1}/organisations/{org_id}/impact", headers=donor).json()
    assert imp["completed_donations"] == 1 and imp["by_unit"][0]["quantity_redistributed"] == 80


def test_unverified_ngo_cannot_claim(client):
    donor, ngo, ngo_org, *_ = setup_actors(client)
    did = published_donation(client, donor)
    uid, unv = make_user("NGO")
    uorg = make_org(uid, "NGO", verified=False, lat=LAT, lon=LON)
    r = claim(client, did, uorg, unv)
    assert r.status_code == 403 and r.json()["error"]["code"] == "ORGANISATION_NOT_ELIGIBLE"
    assert "correlation_id" in r.json()["error"]


def test_second_claim_conflicts_and_idempotent_replay(client):
    donor, ngo, ngo_org, *_ = setup_actors(client)
    did = published_donation(client, donor)
    other_id, other = make_user("NGO")
    other_org = make_org(other_id, "NGO")
    first = claim(client, did, ngo_org, ngo, key="key-aaaaaaaa")
    assert first.status_code == 201
    replay = claim(client, did, ngo_org, ngo, key="key-aaaaaaaa")
    assert replay.status_code == 200 and replay.json()["id"] == first.json()["id"]
    lose = claim(client, did, other_org, other)
    assert lose.status_code == 409 and lose.json()["error"]["code"] == "DONATION_ALREADY_RESERVED"
    did2 = published_donation(client, donor)
    reuse = claim(client, did2, ngo_org, ngo, key="key-aaaaaaaa")
    assert reuse.status_code == 422 and reuse.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Allocation).where(Allocation.donation_id == uuid.UUID(did))) == 1
    assert client.post(f"{V1}/donations/{did}/claim", json={"ngo_org_id": str(ngo_org)}, headers=ngo).status_code == 422  # key required


def test_expired_donation_not_claimable_and_expiry_job(client):
    from app.workers.jobs import expire_donations

    donor, ngo, ngo_org, *_ = setup_actors(client)
    did = published_donation(client, donor)
    with SessionLocal() as db:
        d = db.get(Donation, uuid.UUID(did))
        d.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        d.pickup_end = d.expires_at - timedelta(seconds=1)
        d.pickup_start = d.pickup_end - timedelta(hours=1)
        db.commit()
    r = claim(client, did, ngo_org, ngo)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DONATION_EXPIRED"
    with SessionLocal() as db:
        assert expire_donations(db) == 1
        assert expire_donations(db) == 0
    assert claim(client, did, ngo_org, ngo).json()["error"]["code"] == "DONATION_NOT_AVAILABLE"
    drain_events()
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Notification).where(Notification.event_type == "DonationExpired")) >= 1


def test_permissions(client):
    donor, ngo, ngo_org, vol_id, vol = setup_actors(client)
    assert client.get(f"{V1}/donations").status_code == 401
    assert client.post(f"{V1}/donations", json=donation_payload(), headers=ngo).status_code == 403  # NGO cannot donate
    did = published_donation(client, donor)
    other_id, other_donor = make_user("DONOR")
    make_org(other_id, "DONOR")
    assert client.post(f"{V1}/donations/{did}/withdraw", headers=other_donor).status_code == 403
    assert client.get(f"{V1}/donations/{did}/candidates", headers=other_donor).status_code == 403
    assert client.get(f"{V1}/admin/audit", headers=donor).status_code == 403
    r = claim(client, did, ngo_org, ngo)
    task_id = r.json()["task_id"]
    stranger_id, stranger = make_user("VOLUNTEER")
    assert client.post(f"{V1}/tasks/{task_id}/pickup", headers=stranger).status_code == 403
    assert client.get(f"{V1}/tasks/{task_id}", headers=stranger).status_code == 404
    bad = client.post(f"{V1}/donations", json=donation_payload(pickup_end=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()), headers=donor)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_transition_returns_409(client):
    donor, *_ = setup_actors(client)
    r = client.post(f"{V1}/donations", json=donation_payload(), headers=donor)
    did = r.json()["id"]
    assert client.post(f"{V1}/donations/{did}/withdraw", headers=donor).status_code == 409  # DRAFT cannot be withdrawn
    client.post(f"{V1}/donations/{did}/publish", headers=donor)
    assert client.post(f"{V1}/donations/{did}/publish", headers=donor).status_code == 409


def test_volunteer_decline_and_reassignment_keep_ngo_allocation(client):
    donor, ngo, ngo_org, vol_id, vol = setup_actors(client)
    v2_id, v2 = make_user("VOLUNTEER")
    did = published_donation(client, donor)
    r = claim(client, did, ngo_org, ngo)
    alloc_id, task_id = r.json()["id"], r.json()["task_id"]

    assert client.post(f"{V1}/allocations/{alloc_id}/assign", json={"volunteer_id": str(vol_id)}, headers=ngo).status_code == 200
    assert client.post(f"{V1}/tasks/{task_id}/accept", headers=v2).status_code == 409  # offered to someone else
    assert client.post(f"{V1}/tasks/{task_id}/decline", headers=vol).json()["status"] == "OPEN"
    a = client.get(f"{V1}/allocations/{alloc_id}", headers=ngo).json()
    assert a["status"] == "CONFIRMED" and a["donation_status"] == "RESERVED"  # NGO allocation unchanged
    assert client.post(f"{V1}/tasks/{task_id}/accept", headers=vol).status_code == 409  # cannot retake after declining

    assert client.post(f"{V1}/tasks/{task_id}/accept", headers=v2).json()["status"] == "ACCEPTED"  # self-claim from open pool
    assert client.get(f"{V1}/allocations/{alloc_id}", headers=ngo).json()["donation_status"] == "ASSIGNED"
    assert client.post(f"{V1}/tasks/{task_id}/release", json={"reason": "car broke"}, headers=v2).json()["status"] == "OPEN"
    assert client.get(f"{V1}/allocations/{alloc_id}", headers=ngo).json()["donation_status"] == "REASSIGNMENT_REQUIRED"
    assert client.post(f"{V1}/tasks/{task_id}/accept", headers=vol).status_code == 409
    v3_id, v3 = make_user("VOLUNTEER")
    assert client.post(f"{V1}/tasks/{task_id}/accept", headers=v3).status_code == 200
    assert client.get(f"{V1}/allocations/{alloc_id}", headers=ngo).json()["donation_status"] == "ASSIGNED"


def test_ngo_release_relists_donation(client):
    donor, ngo, ngo_org, *_ = setup_actors(client)
    did = published_donation(client, donor)
    alloc = claim(client, did, ngo_org, ngo).json()
    assert client.post(f"{V1}/allocations/{alloc['id']}/release", headers=ngo).json()["status"] == "RELEASED"
    assert client.get(f"{V1}/donations/{did}", headers=donor).json()["status"] == "PUBLISHED"
    assert claim(client, did, ngo_org, ngo).status_code == 201  # claimable again (new idempotency key)


def test_dispute_flow_and_admin_resolution(client):
    donor, ngo, ngo_org, vol_id, vol = setup_actors(client)
    admin_id, admin = make_user("ADMIN")
    did = published_donation(client, donor)
    r = claim(client, did, ngo_org, ngo).json()
    client.post(f"{V1}/tasks/{r['task_id']}/accept", headers=vol)
    client.post(f"{V1}/tasks/{r['task_id']}/pickup", headers=vol)
    client.post(f"{V1}/tasks/{r['task_id']}/deliver", headers=vol)
    d = client.post(f"{V1}/allocations/{r['id']}/confirm-receipt", json={"received_quantity": 40, "condition_ok": True, "notes": "short"}, headers=ngo).json()
    assert d["status"] == "DISPUTED" and d["donation_status"] == "DISPUTED"
    assert client.post(f"{V1}/admin/donations/{did}/resolve-dispute", json={"outcome": "COMPLETED", "quantity": 40, "reason": "agreed"}, headers=ngo).status_code == 403
    assert client.post(f"{V1}/admin/donations/{did}/resolve-dispute", json={"outcome": "COMPLETED", "quantity": 40, "reason": "agreed partial"}, headers=admin).json()["status"] == "COMPLETED"
    with SessionLocal() as db:
        assert db.scalar(select(ImpactRecord.quantity_redistributed)) == 40
    audit = client.get(f"{V1}/admin/audit", params={"entity_id": did}, headers=admin).json()
    assert audit["total"] >= 7 and all("at" in i for i in audit["items"])  # prior events preserved


def test_admin_verifies_organisation(client):
    ngo_id, ngo = make_user("NGO")
    org = make_org(ngo_id, "NGO", verified=False)
    admin_id, admin = make_user("ADMIN")
    assert client.post(f"{V1}/organisations/{org}/verification", json={"evidence": "registration no. 12345"}, headers=ngo).json()["verification_status"] == "PENDING"
    assert client.get(f"{V1}/admin/organisations", params={"verification_status": "pending"}, headers=admin).json()["items"][0]["id"] == str(org)
    assert client.post(f"{V1}/admin/organisations/{org}/verify", json={"approve": True}, headers=admin).json()["verification_status"] == "VERIFIED"


def test_no_ngo_in_range_then_radius_widens(client):
    """Listing stays PUBLISHED; the eligible radius grows with listing age so a farther NGO becomes eligible."""
    donor_id, donor = make_user("DONOR")
    make_org(donor_id, "DONOR", radius=10)
    ngo_id, ngo = make_user("NGO")
    far_org = make_org(ngo_id, "NGO", lat=LAT + 0.14, lon=LON, radius=40)  # ~15.5 km away
    did = published_donation(client, donor)
    assert client.get(f"{V1}/donations/{did}/candidates", headers=donor).json()["items"] == []
    assert claim(client, did, far_org, ngo).json()["error"]["details"]["reasons"] == ["OUTSIDE_DONOR_RADIUS"]
    with SessionLocal() as db:
        d = db.get(Donation, uuid.UUID(did))
        d.published_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db.commit()
    assert len(client.get(f"{V1}/donations/{did}/candidates", headers=donor).json()["items"]) == 1
    assert claim(client, did, far_org, ngo).status_code == 201
