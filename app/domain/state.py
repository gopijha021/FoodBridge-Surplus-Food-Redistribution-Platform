"""Explicit state machines. Clients never set a status directly; services call transition()."""
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.logging import get_correlation_id
from app.domain.enums import AllocationStatus as A
from app.domain.enums import DonationStatus as D
from app.domain.enums import EntityType
from app.domain.enums import TaskStatus as T

DONATION_RULES: dict[str, set[str]] = {
    D.DRAFT: {D.PUBLISHED, D.CANCELLED},
    D.PUBLISHED: {D.RESERVED, D.EXPIRED, D.WITHDRAWN},
    D.RESERVED: {D.ASSIGNED, D.RELEASED, D.CANCELLED},
    D.RELEASED: {D.PUBLISHED, D.CANCELLED},
    D.ASSIGNED: {D.PICKED_UP, D.REASSIGNMENT_REQUIRED},
    D.REASSIGNMENT_REQUIRED: {D.ASSIGNED, D.CANCELLED},
    D.PICKED_UP: {D.DELIVERED, D.DELIVERY_FAILED},
    D.DELIVERY_FAILED: {D.CANCELLED},  # admin-reviewed
    D.DELIVERED: {D.COMPLETED, D.DISPUTED},
    D.DISPUTED: {D.COMPLETED, D.CANCELLED},  # admin-reviewed
    D.COMPLETED: set(),
    D.EXPIRED: set(),
    D.WITHDRAWN: set(),
    D.CANCELLED: set(),
}

ALLOCATION_RULES: dict[str, set[str]] = {
    A.CONFIRMED: {A.RELEASED, A.COMPLETED, A.DISPUTED, A.CANCELLED},
    A.DISPUTED: {A.COMPLETED, A.CANCELLED},
    A.RELEASED: set(),
    A.COMPLETED: set(),
    A.CANCELLED: set(),
}

TASK_RULES: dict[str, set[str]] = {
    T.OPEN: {T.OFFERED, T.ACCEPTED, T.CANCELLED},
    T.OFFERED: {T.ACCEPTED, T.OPEN, T.CANCELLED},
    T.ACCEPTED: {T.PICKED_UP, T.OPEN, T.CANCELLED},
    T.PICKED_UP: {T.DELIVERED, T.FAILED},
    T.DELIVERED: set(),
    T.FAILED: set(),
    T.CANCELLED: set(),
}

RULES = {EntityType.DONATION: DONATION_RULES, EntityType.ALLOCATION: ALLOCATION_RULES, EntityType.TASK: TASK_RULES}


def can_transition(entity_type: str, from_status: str, to_status: str) -> bool:
    return str(to_status) in {str(x) for x in RULES[EntityType(entity_type)].get(from_status, set())}


def record_history(db: Session, entity_type, entity_id, from_status, to_status, actor_id, reason=None) -> None:
    from app.db.models import StatusHistory

    cid = get_correlation_id()
    db.add(
        StatusHistory(
            entity_type=str(entity_type),
            entity_id=entity_id,
            from_status=from_status,
            to_status=to_status,
            actor_id=actor_id,
            reason=reason,
            correlation_id=None if cid == "-" else cid,
        )
    )


def transition(
    db: Session,
    *,
    entity_type: EntityType,
    entity,
    to_status: str,
    actor_id: uuid.UUID | None,
    reason: str | None = None,
) -> None:
    from_status = str(entity.status)
    if not can_transition(entity_type, from_status, to_status):
        raise AppError(
            "INVALID_STATE_TRANSITION",
            f"Cannot move {entity_type.value.lower()} from {from_status} to {to_status}.",
            409,
            {"from": from_status, "to": str(to_status)},
        )
    entity.status = str(to_status)
    if hasattr(entity, "updated_at"):
        entity.updated_at = datetime.now(timezone.utc)
    record_history(db, entity_type, entity.id, from_status, str(to_status), actor_id, reason)
