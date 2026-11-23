import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.domain.enums import DonationStatus as D
from app.domain.enums import EntityType
from app.domain.state import DONATION_RULES, can_transition, transition
from app.modules.matching.geo import haversine_km
from app.modules.matching.service import check_eligibility, effective_radius_km, score_components

NOW = datetime.now(timezone.utc)


def donation(**kw):
    base = dict(latitude=25.3176, longitude=82.9739, quantity=50, category="PREPARED_MEALS",
                expires_at=NOW + timedelta(hours=4), pickup_end=NOW + timedelta(hours=2),
                published_at=NOW, created_at=NOW)
    base.update(kw)
    return SimpleNamespace(**base)


def org(**kw):
    base = dict(latitude=25.33, longitude=82.99, service_radius_km=10, verification_status="VERIFIED", is_active=True,
                is_blocked=False, capacity=None, accepted_categories=None, cold_storage=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_every_transition_in_blueprint_is_allowed_and_others_are_not():
    assert can_transition(EntityType.DONATION, "DRAFT", "PUBLISHED")
    assert can_transition(EntityType.DONATION, "PUBLISHED", "RESERVED")
    assert can_transition(EntityType.DONATION, "DELIVERED", "DISPUTED")
    assert not can_transition(EntityType.DONATION, "PUBLISHED", "DELIVERED")
    assert not can_transition(EntityType.DONATION, "RESERVED", "PUBLISHED")
    for terminal in (D.COMPLETED, D.EXPIRED, D.WITHDRAWN, D.CANCELLED):
        assert DONATION_RULES[terminal] == set()


def test_transition_rejects_invalid_and_does_not_mutate():
    ent = SimpleNamespace(id=uuid.uuid4(), status="PUBLISHED")
    with pytest.raises(AppError) as e:
        transition(None, entity_type=EntityType.DONATION, entity=ent, to_status="COMPLETED", actor_id=None)
    assert e.value.code == "INVALID_STATE_TRANSITION" and ent.status == "PUBLISHED"


def test_haversine_known_distance():
    assert haversine_km(0, 0, 0, 1) == pytest.approx(111.19, abs=0.1)
    assert haversine_km(25.3, 82.9, 25.3, 82.9) == 0


def test_radius_widens_with_listing_age_and_is_capped():
    donor = SimpleNamespace(service_radius_km=10)
    assert effective_radius_km(donation(), donor, NOW) == 10
    assert effective_radius_km(donation(published_at=NOW - timedelta(minutes=30)), donor, NOW) == pytest.approx(25)
    assert effective_radius_km(donation(published_at=NOW - timedelta(days=2)), donor, NOW) == 50


def test_eligibility_filters():
    donor = SimpleNamespace(service_radius_km=10)
    d = donation()
    assert check_eligibility(d, None, org(), donor, NOW).ok
    assert "ORG_NOT_VERIFIED" in check_eligibility(d, None, org(verification_status="PENDING"), donor, NOW).reasons
    assert "ORG_BLOCKED" in check_eligibility(d, None, org(is_blocked=True), donor, NOW).reasons
    assert "INSUFFICIENT_CAPACITY" in check_eligibility(d, None, org(capacity=10), donor, NOW).reasons
    assert "OUTSIDE_DONOR_RADIUS" in check_eligibility(d, None, org(latitude=26.5), donor, NOW).reasons
    assert "CATEGORY_NOT_ACCEPTED" in check_eligibility(d, None, org(accepted_categories=["PRODUCE"]), donor, NOW).reasons
    cold = SimpleNamespace(storage_type="REFRIGERATED")
    assert "STORAGE_INCOMPATIBLE" in check_eligibility(d, cold, org(), donor, NOW).reasons
    late = donation(pickup_end=NOW + timedelta(minutes=1))
    assert "CANNOT_PICKUP_BEFORE_DEADLINE" in check_eligibility(late, None, org(), donor, NOW).reasons
    assert "DONATION_EXPIRED" in check_eligibility(donation(expires_at=NOW - timedelta(minutes=1)), None, org(), donor, NOW).reasons


def test_score_is_normalised_and_closer_ngo_ranks_higher():
    donor = SimpleNamespace(service_radius_km=10)
    d = donation()
    near, far = org(latitude=25.32, longitude=82.975), org(latitude=25.38, longitude=83.03)
    sn = score_components(d, near, donor, check_eligibility(d, None, near, donor, NOW), NOW, 0.5)
    sf = score_components(d, far, donor, check_eligibility(d, None, far, donor, NOW), NOW, 0.5)
    assert all(0 <= v <= 1 for v in sn.values())
    assert sn["score"] > sf["score"]
