import uuid
from datetime import datetime, timedelta, timezone

from app.core.security import create_access_token
from app.db.models import Organisation, OrganisationMembership, User
from app.db.session import SessionLocal
from app.workers.outbox import publish_pending

LAT, LON = 25.3176, 82.9739  # Varanasi (illustrative)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def make_user(role: str, name: str | None = None) -> tuple[uuid.UUID, dict]:
    """Insert a user directly (fast) and return (id, auth headers)."""
    with SessionLocal() as db:
        u = User(name=name or f"{role.lower()}-{uuid.uuid4().hex[:6]}", email=f"{uuid.uuid4().hex}@example.com",
                 password_hash="x", role=role)
        db.add(u)
        db.commit()
        return u.id, auth(create_access_token(u.id, role))


def make_org(user_id, type_: str, *, verified: bool = True, lat: float = LAT, lon: float = LON, radius: float = 10,
             capacity: float | None = None, **kw) -> uuid.UUID:
    with SessionLocal() as db:
        o = Organisation(name=f"{type_}-{uuid.uuid4().hex[:6]}", type=type_, latitude=lat, longitude=lon,
                         service_radius_km=radius, capacity=capacity,
                         verification_status="VERIFIED" if verified else "UNVERIFIED", **kw)
        db.add(o)
        db.flush()
        db.add(OrganisationMembership(user_id=user_id, organisation_id=o.id))
        db.commit()
        return o.id


def donation_payload(**over) -> dict:
    now = datetime.now(timezone.utc)
    p = {
        "title": "Packed vegetarian meals", "category": "PREPARED_MEALS", "quantity": 80, "unit": "PORTIONS",
        "prepared_at": (now - timedelta(minutes=30)).isoformat(),
        "expires_at": (now + timedelta(hours=5)).isoformat(),
        "pickup_start": (now - timedelta(minutes=5)).isoformat(),
        "pickup_end": (now + timedelta(hours=2)).isoformat(),
        "location": {"latitude": LAT, "longitude": LON}, "allergen_notes": "Contains wheat",
    }
    p.update(over)
    return p


def published_donation(client, donor_headers, **over) -> str:
    r = client.post("/api/v1/donations", json=donation_payload(**over), headers=donor_headers)
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    r = client.post(f"/api/v1/donations/{did}/publish", headers=donor_headers)
    assert r.status_code == 200, r.text
    return did


def claim(client, donation_id, ngo_org_id, headers, key=None):
    return client.post(f"/api/v1/donations/{donation_id}/claim", json={"ngo_org_id": str(ngo_org_id)},
                       headers={**headers, "Idempotency-Key": key or uuid.uuid4().hex})


def drain_events(max_rounds: int = 10) -> int:
    """Run the outbox publisher until there is nothing left (consumers may emit follow-up events)."""
    total = 0
    for _ in range(max_rounds):
        with SessionLocal() as db:
            n = publish_pending(db)
        total += n
        if n == 0:
            break
    return total
