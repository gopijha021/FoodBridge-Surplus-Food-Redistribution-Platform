import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, model_validator


class LocationIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


class DonationCreateIn(BaseModel):
    donor_org_id: uuid.UUID | None = None  # required only if the user belongs to several donor orgs
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=50)
    quantity: float = Field(gt=0)
    unit: str = Field(min_length=1, max_length=20)
    prepared_at: datetime
    expires_at: datetime
    pickup_start: datetime
    pickup_end: datetime
    location: LocationIn
    storage_type: str | None = Field(default=None, pattern="^(AMBIENT|REFRIGERATED|FROZEN)$")
    packaging: str | None = Field(default=None, max_length=100)
    allergen_notes: str | None = None
    dietary_tags: list[str] | None = None
    handling_notes: str | None = None

    @model_validator(mode="after")
    def _check_times(self):
        for f in ("prepared_at", "expires_at", "pickup_start", "pickup_end"):
            setattr(self, f, _utc(getattr(self, f)))
        if self.expires_at <= self.prepared_at:
            raise ValueError("expires_at must be after prepared_at")
        if self.pickup_end <= self.pickup_start:
            raise ValueError("pickup_end must be after pickup_start")
        if self.pickup_end > self.expires_at:
            raise ValueError("pickup_end must not be after expires_at")
        return self


class DonationOut(BaseModel):
    id: str
    donor_org_id: str
    title: str
    category: str
    quantity: float
    unit: str
    prepared_at: datetime
    expires_at: datetime
    pickup_start: datetime
    pickup_end: datetime
    location: dict
    status: str
    version: int
    storage_type: str | None = None
    packaging: str | None = None
    allergen_notes: str | None = None
    dietary_tags: list[str] | None = None
    handling_notes: str | None = None


class ClaimIn(BaseModel):
    ngo_org_id: uuid.UUID


class ReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class AllocationOut(BaseModel):
    id: str
    donation_id: str
    ngo_org_id: str
    allocated_quantity: float
    status: str
    donation_status: str
    task_id: str | None = None
