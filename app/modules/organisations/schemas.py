from pydantic import BaseModel, Field


class OrgCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = Field(pattern="^(DONOR|NGO)$")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    service_radius_km: float = Field(default=10.0, gt=0, le=200)
    capacity: float | None = Field(default=None, gt=0)
    accepted_categories: list[str] | None = None
    cold_storage: bool = False


class OrgOut(BaseModel):
    id: str
    name: str
    type: str
    verification_status: str
    service_radius_km: float
    is_active: bool
    latitude: float | None = None  # exact coordinates only for members/admin
    longitude: float | None = None
    capacity: float | None = None
    accepted_categories: list[str] | None = None
    cold_storage: bool | None = None


class VerificationIn(BaseModel):
    evidence: str = Field(min_length=5, max_length=5000)
