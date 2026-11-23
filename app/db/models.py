from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Text,
    UniqueConstraint, Uuid, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Always store UTC; always return timezone-aware UTC (SQLite drops tzinfo)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Organisation(Base):
    __tablename__ = "organisations"
    __table_args__ = (
        CheckConstraint("service_radius_km > 0", name="radius_positive"),
        CheckConstraint("latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180", name="coords_valid"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(10))  # DONOR / NGO
    verification_status: Mapped[str] = mapped_column(String(12), default="UNVERIFIED")
    verification_evidence: Mapped[str | None] = mapped_column(Text)
    service_radius_km: Mapped[float] = mapped_column(Float, default=10.0)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)  # suspended by admin
    capacity: Mapped[float | None] = mapped_column(Float)  # NGO: max quantity it can take per donation
    accepted_categories: Mapped[list | None] = mapped_column(JSON)  # NGO: null = all
    cold_storage: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OrganisationMembership(Base):
    __tablename__ = "organisation_memberships"
    __table_args__ = (UniqueConstraint("user_id", "organisation_id"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    organisation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Donation(Base):
    __tablename__ = "donations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("pickup_end > pickup_start", name="pickup_window_valid"),
        CheckConstraint("expires_at > prepared_at", name="expiry_after_prepared"),
        Index("ix_donations_status_expires_at", "status", "expires_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    donor_org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"), index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50))
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(20))
    prepared_at: Mapped[datetime] = mapped_column(UTCDateTime)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    pickup_start: Mapped[datetime] = mapped_column(UTCDateTime)
    pickup_end: Mapped[datetime] = mapped_column(UTCDateTime)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT")
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # optimistic versioning: SQLAlchemy bumps this on every UPDATE and raises StaleDataError on lost updates
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    __mapper_args__ = {"version_id_col": version}


class FoodDetails(Base):
    __tablename__ = "food_details"
    donation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("donations.id"), primary_key=True)
    storage_type: Mapped[str | None] = mapped_column(String(20))  # AMBIENT / REFRIGERATED / FROZEN
    packaging: Mapped[str | None] = mapped_column(String(100))
    allergen_notes: Mapped[str | None] = mapped_column(Text)
    dietary_tags: Mapped[list | None] = mapped_column(JSON)
    handling_notes: Mapped[str | None] = mapped_column(Text)


_ACTIVE = "status NOT IN ('RELEASED','CANCELLED')"


class Allocation(Base):
    __tablename__ = "allocations"
    __table_args__ = (
        # Second line of defence: at most one live allocation per donation (single-recipient MVP).
        # Partial so a RELEASED / CANCELLED allocation does not block re-listing the donation.
        Index("uq_allocations_active_donation", "donation_id", unique=True,
              postgresql_where=text(_ACTIVE), sqlite_where=text(_ACTIVE)),
        UniqueConstraint("claimed_by", "idempotency_key", name="uq_allocations_user_idempotency"),
        Index("ix_allocations_ngo_org_id_created_at", "ngo_org_id", "created_at"),
        CheckConstraint("allocated_quantity > 0", name="allocated_positive"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    donation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("donations.id"))
    ngo_org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"))
    claimed_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    allocated_quantity: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(20), default="CONFIRMED")
    idempotency_key: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DeliveryTask(Base):
    __tablename__ = "delivery_tasks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    allocation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("allocations.id"), unique=True)
    volunteer_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="OPEN", index=True)
    declined_volunteer_ids: Mapped[list] = mapped_column(JSON, default=list)
    offered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    pickup_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class StatusHistory(Base):
    """Append-only audit trail. Never updated or deleted by application code."""

    __tablename__ = "status_history"
    __table_args__ = (Index("ix_status_history_entity", "entity_type", "entity_id", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[str] = mapped_column(String(20))
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # null = system
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class VolunteerLocation(Base):
    __tablename__ = "volunteer_locations"
    __table_args__ = (Index("ix_volunteer_locations_vol_time", "volunteer_id", "recorded_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    volunteer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(10), default="PENDING", index=True)  # PENDING/SENT/FAILED
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_unpublished", "published_at", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    aggregate_type: Mapped[str] = mapped_column(String(30))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ProcessedEvent(Base):
    """Consumer idempotency ledger: (event, consumer) is processed at most once."""

    __tablename__ = "processed_events"
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    consumer: Mapped[str] = mapped_column(String(60), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DeadLetterEvent(Base):
    __tablename__ = "dead_letter_events"
    id: Mapped[uuid.UUID] = _uuid_pk()
    event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    event_type: Mapped[str | None] = mapped_column(String(50))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ImpactRecord(Base):
    __tablename__ = "impact_records"
    __table_args__ = (CheckConstraint("quantity_redistributed >= 0", name="qty_non_negative"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    donation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("donations.id"), unique=True)
    donor_org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"), index=True)
    ngo_org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organisations.id"), index=True)
    quantity_redistributed: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(20))
    verified_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    source: Mapped[str] = mapped_column(String(30), default="NGO_CONFIRMED")
