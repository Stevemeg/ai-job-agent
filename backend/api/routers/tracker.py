"""Application-tracker endpoints over the Postgres events layer."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..deps import load_profile
from ..schemas import EventCreate, TrackedJob

router = APIRouter(prefix="/tracker", tags=["tracker"])


def _user_id():
    # DB imports deferred: the API must boot (and non-tracker routes work)
    # with Postgres down; only these endpoints require it.
    try:
        from ...database.db import init_db
        from ...database.tracker import get_or_create_user
        init_db()
    except Exception as exc:                                # noqa: BLE001
        raise HTTPException(
            503, f"Tracker database unavailable ({exc.__class__.__name__}). "
                 "Start it: cd backend/database && docker compose up -d")
    profile = load_profile()
    return get_or_create_user(profile.get("email") or "local@user",
                              profile.get("name", ""))


@router.get("", response_model=list[TrackedJob])
def tracked_jobs():
    user_id = _user_id()
    from ...database.tracker import get_tracked_jobs
    return get_tracked_jobs(user_id)


@router.post("/events", status_code=201)
def create_event(event: EventCreate):
    user_id = _user_id()
    from ...database.events import log_outcome
    event_id = log_outcome(
        user_id, event.company, event.title, event.event_type,
        match_score=event.match_score, score_breakdown=event.score_breakdown,
        note=event.note)
    return {"event_id": event_id}
