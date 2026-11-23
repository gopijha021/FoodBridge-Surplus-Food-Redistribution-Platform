import asyncio
import json
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import locations
from app.core.deps import get_current_user, require_roles
from app.core.errors import AppError
from app.core.ratelimit import RateLimiter
from app.db.models import Allocation, DeliveryTask, Donation, StatusHistory, User
from app.db.session import SessionLocal, get_db
from app.modules.delivery import service

router = APIRouter(tags=["delivery"])
location_limit = RateLimiter("location", 60, 60)


class LocationIn(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class ReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class FailIn(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


def task_out(t: DeliveryTask) -> dict:
    return {"id": str(t.id), "allocation_id": str(t.allocation_id), "status": t.status,
            "volunteer_id": str(t.volunteer_id) if t.volunteer_id else None,
            "offered_at": t.offered_at, "accepted_at": t.accepted_at, "pickup_at": t.pickup_at,
            "delivered_at": t.delivered_at, "failure_reason": t.failure_reason}


Volunteer = Depends(require_roles("VOLUNTEER"))


@router.post("/volunteers/me/location", status_code=204, dependencies=[Depends(location_limit)])
def update_my_location(body: LocationIn, db: Session = Depends(get_db), user: User = Volunteer):
    service.record_location(db, user, body.latitude, body.longitude)


@router.get("/tasks/open")
def open_tasks(latitude: float = Query(..., ge=-90, le=90), longitude: float = Query(..., ge=-180, le=180),
               radius_km: float = Query(15, gt=0, le=100), db: Session = Depends(get_db), user: User = Volunteer):
    return {"items": service.open_tasks_for_volunteer(db, user, latitude, longitude, radius_km)}


def _viewable(db: Session, user: User, task_id: uuid.UUID):
    task = service.get_task_or_404(db, task_id)
    alloc = db.get(Allocation, task.allocation_id)
    donation = db.get(Donation, alloc.donation_id)
    if not service.can_view_task(db, user, task, alloc, donation):
        raise AppError("TASK_NOT_FOUND", "Task not found.", 404)  # do not leak existence
    return task, alloc, donation


@router.get("/tasks/{task_id}")
def get_task(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task, alloc, donation = _viewable(db, user, task_id)
    return {**task_out(task), "donation_id": str(donation.id), "ngo_org_id": str(alloc.ngo_org_id)}


@router.post("/tasks/{task_id}/accept")
def accept(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Volunteer):
    return task_out(service.accept(db, user, task_id))


@router.post("/tasks/{task_id}/decline")
def decline(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Volunteer):
    return task_out(service.decline(db, user, task_id))


@router.post("/tasks/{task_id}/release")
def release(task_id: uuid.UUID, body: ReasonIn | None = None, db: Session = Depends(get_db), user: User = Volunteer):
    return task_out(service.release(db, user, task_id, body.reason if body else None))


@router.post("/tasks/{task_id}/pickup")
def pickup(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Volunteer):
    return task_out(service.pickup(db, user, task_id))


@router.post("/tasks/{task_id}/deliver")
def deliver(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Volunteer):
    return task_out(service.deliver(db, user, task_id))


@router.post("/tasks/{task_id}/fail")
def fail(task_id: uuid.UUID, body: FailIn, db: Session = Depends(get_db), user: User = Volunteer):
    return task_out(service.fail(db, user, task_id, body.reason))


@router.post("/tasks/{task_id}/location", status_code=204, dependencies=[Depends(location_limit)])
def task_location(task_id: uuid.UUID, body: LocationIn, db: Session = Depends(get_db), user: User = Volunteer):
    task = service.get_task_or_404(db, task_id)
    if task.volunteer_id != user.id or task.status not in ("ACCEPTED", "PICKED_UP"):
        raise AppError("FORBIDDEN_TASK", "Location can only be shared for your active task.", 403)
    service.record_location(db, user, body.latitude, body.longitude)


@router.get("/tasks/{task_id}/location")
def get_task_location(task_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task, _, _ = _viewable(db, user, task_id)
    if task.status not in ("ACCEPTED", "PICKED_UP") or not task.volunteer_id:
        raise AppError("LOCATION_NOT_AVAILABLE", "Live location is only available during an active task.", 404)
    return locations.get_latest(str(task.volunteer_id)) or {}


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: uuid.UUID, request: Request, user: User = Depends(get_current_user)):
    """Server-Sent Events: task/donation status changes + latest courier location (never a raw GPS firehose)."""

    def load() -> dict:
        with SessionLocal() as db:
            task, alloc, donation = _viewable(db, user, task_id)
            return {"ids": [task.id, alloc.id, donation.id], "volunteer": task.volunteer_id, "status": task.status}

    ctx = await run_in_threadpool(load)  # also enforces access control

    def fetch_new(seen: set) -> list:
        with SessionLocal() as db:
            rows = db.scalars(select(StatusHistory).where(StatusHistory.entity_id.in_(ctx["ids"])).order_by(StatusHistory.created_at)).all()
            return [r for r in rows if r.id not in seen]

    async def gen():
        seen: set = set()
        last_loc = None
        for _ in range(150):  # ~5 minutes at 2s; clients reconnect
            if await request.is_disconnected():
                break
            for r in await run_in_threadpool(fetch_new, seen):
                seen.add(r.id)
                data = {"entity_type": r.entity_type, "from": r.from_status, "to": r.to_status, "at": r.created_at.isoformat()}
                yield f"event: status\ndata: {json.dumps(data)}\n\n"
            if ctx["volunteer"]:
                loc = locations.get_latest(str(ctx["volunteer"]))
                if loc and loc != last_loc:
                    last_loc = loc
                    yield f"event: location\ndata: {json.dumps(loc)}\n\n"
            yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
