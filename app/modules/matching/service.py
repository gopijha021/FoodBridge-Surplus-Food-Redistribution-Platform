"""Explainable matching: hard eligibility filters first, then a weighted, configurable score.
Ranking never overrides eligibility."""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Allocation, Donation, FoodDetails, Organisation
from app.modules.matching.geo import haversine_km, radius_clause

log = logging.getLogger("foodbridge.matching")


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class Eligibility:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    distance_km: float = 0.0
    eta_minutes: float = 0.0


def effective_radius_km(donation: Donation, donor_org: Organisation, now: datetime) -> float:
    """Donor's allowed radius, widened gradually while the listing stays unclaimed.
    Deterministic from listing age so matching, claiming and the worker always agree."""
    base = donor_org.service_radius_km or settings.donation_default_radius_km
    start = donation.published_at or donation.created_at
    minutes = max(0.0, (now - start).total_seconds() / 60.0)
    steps = int(minutes // max(1, settings.radius_widen_every_minutes))
    cap = max(settings.donation_max_radius_km, base)
    return min(cap, base * (1 + settings.radius_widen_factor * steps))


def check_eligibility(
    donation: Donation, food: FoodDetails | None, ngo: Organisation, donor_org: Organisation, now: datetime
) -> Eligibility:
    reasons: list[str] = []
    if ngo.verification_status != "VERIFIED" or not ngo.is_active:
        reasons.append("ORG_NOT_VERIFIED")
    if ngo.is_blocked:
        reasons.append("ORG_BLOCKED")
    dist = haversine_km(donation.latitude, donation.longitude, ngo.latitude, ngo.longitude)
    if dist > effective_radius_km(donation, donor_org, now):
        reasons.append("OUTSIDE_DONOR_RADIUS")
    if dist > ngo.service_radius_km:
        reasons.append("OUTSIDE_NGO_SERVICE_RADIUS")
    if ngo.capacity is not None and ngo.capacity < donation.quantity:
        reasons.append("INSUFFICIENT_CAPACITY")
    if donation.expires_at <= now:
        reasons.append("DONATION_EXPIRED")
    eta = dist / max(settings.avg_courier_speed_kmh, 1.0) * 60.0
    if now + timedelta(minutes=eta) > donation.pickup_end:
        reasons.append("CANNOT_PICKUP_BEFORE_DEADLINE")
    if ngo.accepted_categories and donation.category not in ngo.accepted_categories:
        reasons.append("CATEGORY_NOT_ACCEPTED")
    if food and food.storage_type in ("REFRIGERATED", "FROZEN") and not ngo.cold_storage:
        reasons.append("STORAGE_INCOMPATIBLE")
    return Eligibility(not reasons, reasons, dist, eta)


def score_components(
    donation: Donation, ngo: Organisation, donor_org: Organisation, elig: Eligibility, now: datetime, fulfilment: float
) -> dict[str, float]:
    radius = max(effective_radius_km(donation, donor_org, now), 0.001)
    remaining = (donation.pickup_end - now).total_seconds() / 60.0
    comps = {
        "proximity_score": _clamp(1 - elig.distance_km / radius),
        "capacity_fit": 1.0 if not ngo.capacity else _clamp(donation.quantity / ngo.capacity),
        "pickup_readiness": _clamp(1 - elig.eta_minutes / remaining) if remaining > 0 else 0.0,
        "historical_fulfilment": _clamp(fulfilment),
    }
    comps["score"] = (
        settings.weight_proximity * comps["proximity_score"]
        + settings.weight_capacity_fit * comps["capacity_fit"]
        + settings.weight_pickup_readiness * comps["pickup_readiness"]
        + settings.weight_historical * comps["historical_fulfilment"]
    )
    return {k: round(v, 4) for k, v in comps.items()}


def fulfilment_rates(db: Session, org_ids: list) -> dict:
    if not org_ids:
        return {}
    done = func.sum(case((Allocation.status == "COMPLETED", 1), else_=0))
    closed = func.sum(case((Allocation.status.in_(["COMPLETED", "RELEASED", "CANCELLED", "DISPUTED"]), 1), else_=0))
    rows = db.execute(
        select(Allocation.ngo_org_id, done, closed).where(Allocation.ngo_org_id.in_(org_ids)).group_by(Allocation.ngo_org_id)
    ).all()
    rates = {oid: 0.5 for oid in org_ids}  # neutral prior for NGOs with no history
    for oid, d, c in rows:
        if c:
            rates[oid] = (d or 0) / c
    return rates


def rank_candidates(db: Session, donation: Donation, now: datetime | None = None, limit: int = 20) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    donor_org = db.get(Organisation, donation.donor_org_id)
    food = db.get(FoodDetails, donation.id)
    radius = effective_radius_km(donation, donor_org, now)
    ngos = db.scalars(
        select(Organisation).where(
            Organisation.type == "NGO",
            Organisation.verification_status == "VERIFIED",
            Organisation.is_active.is_(True),
            Organisation.is_blocked.is_(False),
            radius_clause(Organisation.latitude, Organisation.longitude, donation.latitude, donation.longitude,
                          radius, db.bind.dialect.name),
        )
    ).all()
    rates = fulfilment_rates(db, [n.id for n in ngos])
    ranked = []
    for ngo in ngos:
        elig = check_eligibility(donation, food, ngo, donor_org, now)
        if not elig.ok:
            continue
        comps = score_components(donation, ngo, donor_org, elig, now, rates.get(ngo.id, 0.5))
        ranked.append({"ngo_org_id": ngo.id, "name": ngo.name, "distance_km": round(elig.distance_km, 2), **comps})
    ranked.sort(key=lambda r: r["score"], reverse=True)
    for r in ranked[:limit]:  # logged so an admin can see why an NGO ranked first
        log.info("candidate ranked", extra={"extra_fields": {"donation_id": str(donation.id), **{k: str(v) for k, v in r.items()}}})
    return ranked[:limit]


def nearby_donations_for_ngo(
    db: Session, ngo: Organisation, lat: float, lon: float, radius_km: float, now: datetime | None = None, limit: int = 50
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    donations = db.scalars(
        select(Donation).where(
            Donation.status == "PUBLISHED",
            Donation.expires_at > now,
            Donation.pickup_end > now,
            radius_clause(Donation.latitude, Donation.longitude, lat, lon, radius_km, db.bind.dialect.name),
        ).order_by(Donation.expires_at).limit(500)
    ).all()
    rate = fulfilment_rates(db, [ngo.id]).get(ngo.id, 0.5)
    out = []
    for d in donations:
        donor_org = db.get(Organisation, d.donor_org_id)
        food = db.get(FoodDetails, d.id)
        elig = check_eligibility(d, food, ngo, donor_org, now)
        if not elig.ok:
            continue
        comps = score_components(d, ngo, donor_org, elig, now, rate)
        out.append({"donation": d, "distance_km": round(elig.distance_km, 2), **comps})
    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]
