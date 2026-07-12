"""
tracker.py -- read-side queries for the Application Tracker.

The write side has existed since Phase 2 (events.py). This module derives
the tracker's view of the world from the append-only events log:

- A job is "tracked" once it has any deliberate event (save/applied/
  interview/rejected/offer/dismiss). Impressions and clicks are ambient
  telemetry, not tracking.
- A job's STATUS is derived, not stored: the furthest pipeline stage
  reached -- unless the most recent deliberate event is terminal
  (rejected/dismiss), which overrides. This means status never needs
  migration and is always consistent with history.
"""
from __future__ import annotations

from typing import Any

from .db import get_session, CanonicalJob, Event, User

# Deliberate events only -- impressions/clicks don't put a job on the board.
TRACKED_TYPES = ("save", "applied", "interview", "rejected", "offer", "dismiss")

# Pipeline progression for status derivation.
STAGE_ORDER = {"save": 1, "applied": 2, "interview": 3, "offer": 4}
TERMINAL = {"rejected", "dismiss"}


def get_or_create_user(email: str, display_name: str = "") -> int:
    with get_session() as session:
        user = session.query(User).filter_by(email=email).first()
        if user:
            return user.id
        user = User(email=email, display_name=display_name or email.split("@")[0])
        session.add(user)
        session.flush()
        return user.id


def derive_status(event_types_newest_first: list[str]) -> str:
    """Furthest stage reached, unless the newest deliberate event is terminal."""
    deliberate = [e for e in event_types_newest_first if e in TRACKED_TYPES]
    if not deliberate:
        return "save"
    if deliberate[0] in TERMINAL:
        return deliberate[0]
    stages = [e for e in deliberate if e in STAGE_ORDER]
    return max(stages, key=lambda e: STAGE_ORDER[e]) if stages else deliberate[0]


def get_tracked_jobs(user_id: int) -> list[dict[str, Any]]:
    """All tracked jobs for a user with derived status + full history.

    Returns newest-activity-first:
    [{"company", "title", "apply_link", "status", "match_score",
      "last_activity", "history": [{"event_type", "created_at", "note"}]}]
    """
    with get_session() as session:
        rows = (session.query(Event, CanonicalJob)
                .join(CanonicalJob, Event.job_id == CanonicalJob.id)
                .filter(Event.user_id == user_id,
                        Event.event_type.in_(TRACKED_TYPES))
                .order_by(Event.created_at.desc())
                .all())

        by_job: dict[int, dict[str, Any]] = {}
        for event, job in rows:
            entry = by_job.setdefault(job.id, {
                "company": job.company,
                "title": job.title,
                "apply_link": job.apply_link,
                "match_score": None,
                "last_activity": event.created_at,
                "history": [],
            })
            entry["history"].append({
                "event_type": event.event_type,
                "created_at": event.created_at,
                "note": event.note,
            })
            if event.match_score is not None and entry["match_score"] is None:
                entry["match_score"] = float(event.match_score)

        tracked = []
        for entry in by_job.values():
            entry["status"] = derive_status(
                [h["event_type"] for h in entry["history"]])
            tracked.append(entry)

        tracked.sort(key=lambda e: e["last_activity"], reverse=True)
        return tracked


def get_funnel_counts(tracked: list[dict]) -> dict[str, int]:
    """Pipeline counts from an already-fetched tracked list (pure)."""
    counts = {"save": 0, "applied": 0, "interview": 0, "offer": 0,
              "rejected": 0, "dismiss": 0}
    for t in tracked:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return counts
