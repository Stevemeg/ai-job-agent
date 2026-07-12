"""
events.py — the actual logging functions Phase 2 exists for.

log_impressions()  : call automatically every time rank_jobs() produces a
                      ranked list, so every job a user sees gets recorded.
log_outcome()       : call manually (e.g. from a small CLI prompt or future
                      API endpoint) when something real happens -- applied,
                      got an interview, rejected, offer.

Both functions upsert the job into canonical_jobs first (insert if new,
update last_seen_at if already known), so events always have a stable job_id
to point to, regardless of how many times the same job gets re-scraped.
"""
from .db import get_session, CanonicalJob, Event, VALID_EVENT_TYPES


def _get_or_create_job(session, job_dict):
    """Find the canonical_jobs row for this job, or create it. Returns the
    CanonicalJob ORM object (with a real .id) either way."""
    company = (job_dict.get("company") or "").strip()
    normalized_title = CanonicalJob.normalize_title(job_dict.get("title", ""))

    existing = (session.query(CanonicalJob)
                .filter_by(company=company, normalized_title=normalized_title)
                .first())
    if existing:
        from sqlalchemy import func
        existing.last_seen_at = func.now()
        return existing

    job = CanonicalJob(
        company=company,
        title=job_dict.get("title"),
        normalized_title=normalized_title,
        location=job_dict.get("location"),
        tags=job_dict.get("tags") or [],
        experience=job_dict.get("experience"),
        seniority=job_dict.get("seniority"),
        clean_description=job_dict.get("clean_description"),
        apply_link=job_dict.get("apply_link"),
        source=job_dict.get("source"),
    )
    session.add(job)
    session.flush()   # populate job.id before returning
    return job


def log_impressions(user_id, ranked_jobs):
    """Logs ONE 'impression' event per job in a ranked-results list.

    ranked_jobs is the list rank_jobs() already returns -- each item must
    have at least: title, company, match_score, score_breakdown, and ideally
    the other normalized fields (location/tags/etc) for canonical_jobs to be
    fully populated on first sight.

    Call this every time a user views a ranked list -- it's the auto-logged
    half of the events log.
    """
    logged = 0
    with get_session() as session:
        for job in ranked_jobs:
            canonical = _get_or_create_job(session, job)
            event = Event(
                user_id=user_id,
                job_id=canonical.id,
                event_type="impression",
                match_score=job.get("match_score"),
                score_breakdown=job.get("score_breakdown"),
            )
            session.add(event)
            logged += 1
    return logged


def log_outcome(user_id, company, title, event_type, match_score=None,
                score_breakdown=None, note=None):
    """Logs a manual outcome event (applied / interview / rejected / offer /
    save / dismiss / click) for a job identified by company + title.

    Raises ValueError for an invalid event_type BEFORE hitting the DB, so the
    caller gets a clear Python error instead of a raw SQL constraint error.
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"'{event_type}' is not a valid event_type. Must be one of: {VALID_EVENT_TYPES}"
        )

    with get_session() as session:
        canonical = _get_or_create_job(session, {"company": company, "title": title})
        event = Event(
            user_id=user_id,
            job_id=canonical.id,
            event_type=event_type,
            match_score=match_score,
            score_breakdown=score_breakdown,
            note=note,
        )
        session.add(event)
        session.flush()
        return event.id