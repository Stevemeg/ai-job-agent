"""
store.py -- the agent's reads and writes: triage, the applications rows,
their artifacts, the 'drafted' event and the approval expiry task. Plain
SQL over the app's SQLAlchemy engine; every write is one short
transaction.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from ..database.db import APPLICATION_DONE_STATUSES, get_engine, init_db
from .locations import LocationPolicy, job_eligibility

APPLICATION_KIND = "application"
APPLICATION_MAX_ATTEMPTS = 3
# Phase 5: an application nobody decided on within AGENT_APPROVAL_TTL_DAYS
# expires (one task per documents version).
APPROVAL_EXPIRY_KIND = "approval_expiry"
APPROVAL_EXPIRY_MAX_ATTEMPTS = 3
# Statuses persist() may (re-)draft from: never after a decision.
_DRAFTABLE = ("queued", "drafting", "awaiting_approval")

# Events that take a job off the agent's list for good.
EXCLUDING_EVENTS = ("applied", "dismiss", "declined")


def application_thread(application_id: int) -> str:
    return f"application:{application_id}"


def application_idempotency_key(application_id: int) -> str:
    return f"application:{application_id}"


def expiry_idempotency_key(application_id: int, version: int) -> str:
    return f"expiry:{application_id}:{version}"


def _approval_ttl_days() -> int:
    from .settings import AgentSettings, settings_from_env
    try:
        return settings_from_env().approval_ttl_days
    except ValueError:            # a malformed value never blocks a draft
        return AgentSettings().approval_ttl_days


def enqueue_expiry(application_id: int, version: int, *, conn: Any,
                   ttl_days: int | None = None) -> int:
    """Queue the "approval_expiry" task for this version, due in
    AGENT_APPROVAL_TTL_DAYS (database clock), in the caller's transaction.
    Keyed "expiry:<application_id>:<version>": once per version."""
    from .. import taskqueue
    days = _approval_ttl_days() if ttl_days is None else ttl_days
    due = conn.execute(text("SELECT now() + make_interval(days => :d)"),
                       {"d": days}).scalar()
    return taskqueue.enqueue(
        APPROVAL_EXPIRY_KIND, {"application_id": application_id, "version": version},
        idempotency_key=expiry_idempotency_key(application_id, version),
        run_after=due, max_attempts=APPROVAL_EXPIRY_MAX_ATTEMPTS, conn=conn)


def user_for_profile(profile: dict) -> int:
    """The users row for this profile -- the same identity the tracker
    uses (its email, else "local@user")."""
    from ..database.tracker import get_or_create_user
    init_db()
    return get_or_create_user(profile.get("email") or "local@user",
                              profile.get("name", ""))


# --------------------------------------------------------------------------
# triage
# --------------------------------------------------------------------------
_TRIAGE = text("""
    SELECT s.job_id, s.match_score, j.title, j.company, j.location, j.postings,
           j.apply_link
    FROM job_scores s
    JOIN canonical_jobs j ON j.id = s.job_id
    WHERE s.profile_hash = :profile_hash
      AND s.cache_key = :cache_key
      AND s.passed
      AND s.match_score >= :min_score
      AND j.content_hash IS NOT NULL
      AND j.last_seen_at >= now() - make_interval(days => :max_age_days)
      AND NOT EXISTS (SELECT 1 FROM applications a
                      WHERE a.user_id = :user_id AND a.job_id = s.job_id)
      AND NOT EXISTS (SELECT 1 FROM events e
                      WHERE e.user_id = :user_id AND e.job_id = s.job_id
                        AND e.event_type IN :excluding)
    ORDER BY s.match_score DESC, s.job_id
""").bindparams(bindparam("excluding", expanding=True))


def triage(*, user_id: int, profile_hash: str, cache_key: str,
           min_score: float, max_age_days: int, cap: int,
           policy: LocationPolicy | None = None) -> list[dict]:
    """The jobs to draft for, best first: scored for THIS profile under
    THIS cache key, passed the gates, score >= min_score, seen within
    max_age_days, no application yet, no applied/dismiss/declined event,
    and in a location the policy accepts (locations.py; default: the
    AGENT_LOCATIONS defaults, no global remote). Ties keep job id order.
    At most `cap`.

    Each candidate carries the posting its application will use
    (`posting`, None for the job's own location) and the resulting
    `location` and `apply_link`."""
    if cap <= 0:
        return []
    policy = policy or LocationPolicy()
    init_db()
    with get_engine().connect() as conn:
        rows = conn.execute(_TRIAGE, {
            "profile_hash": profile_hash, "cache_key": cache_key,
            "min_score": min_score, "max_age_days": max_age_days,
            "user_id": user_id, "excluding": list(EXCLUDING_EVENTS)}).mappings()
        picked = []
        for r in rows:
            fit = job_eligibility(r["location"], r["postings"], policy)
            if not fit.eligible:
                continue
            posting = fit.posting or {}
            picked.append({
                "job_id": r["job_id"], "match_score": float(r["match_score"]),
                "title": r["title"], "company": r["company"],
                "location": posting.get("location") or r["location"],
                "apply_link": posting.get("apply_link") or r["apply_link"],
                "location_match": fit.match, "posting": fit.posting})
            if len(picked) >= cap:
                break
    return picked


# --------------------------------------------------------------------------
# dispatch: the applications row and its task, in one transaction
# --------------------------------------------------------------------------
_INSERT_APPLICATION = text("""
    INSERT INTO applications (id, user_id, job_id, profile_hash, cache_key,
                              match_score, status, thread_id, posting)
    SELECT n, :user_id, :job_id, :profile_hash, :cache_key, :match_score,
           'queued', 'application:' || n, :posting
    FROM (SELECT nextval(pg_get_serial_sequence('applications', 'id')) AS n) AS seq
    ON CONFLICT (user_id, job_id) DO NOTHING
    RETURNING id
""").bindparams(bindparam("posting", type_=JSONB(none_as_null=True)))


def dispatch(*, user_id: int, job_id: int, match_score: float,
             profile_hash: str, cache_key: str,
             posting: dict | None = None) -> dict | None:
    """Create the application and enqueue its "application" task (key
    "application:<id>") in ONE transaction: both commit or neither does.
    `posting`: the posting triage matched (its location and apply_link
    become the application's), None for the job's own. None if (user,
    job) already has an application."""
    from .. import taskqueue
    init_db()
    with get_engine().begin() as conn:
        application_id = conn.execute(_INSERT_APPLICATION, {
            "user_id": user_id, "job_id": job_id, "match_score": match_score,
            "profile_hash": profile_hash, "cache_key": cache_key,
            "posting": posting}).scalar()
        if application_id is None:
            return None
        task_id = taskqueue.enqueue(
            APPLICATION_KIND, {"application_id": application_id},
            idempotency_key=application_idempotency_key(application_id),
            max_attempts=APPLICATION_MAX_ATTEMPTS, conn=conn)
    return {"application_id": application_id, "task_id": task_id,
            "job_id": job_id, "match_score": match_score}


# --------------------------------------------------------------------------
# the application graph's reads and writes
# --------------------------------------------------------------------------
_LOAD = text("""
    SELECT a.id, a.user_id, a.job_id, a.status, a.match_score, a.profile_hash,
           a.cache_key, j.title, j.company,
           COALESCE(a.posting->>'location', j.location) AS location,
           COALESCE(a.posting->>'apply_link', j.apply_link) AS apply_link,
           j.clean_description, j.tags, j.seniority
    FROM applications a JOIN canonical_jobs j ON j.id = a.job_id
    WHERE a.id = :id
""")


class ApplicationNotFound(LookupError):
    pass


def load_application(application_id: int) -> dict:
    init_db()
    with get_engine().connect() as conn:
        row = conn.execute(_LOAD, {"id": application_id}).mappings().first()
    if row is None:
        raise ApplicationNotFound(f"application {application_id} does not exist")
    out = dict(row)
    out["match_score"] = None if row["match_score"] is None else float(row["match_score"])
    out["tags"] = list(row["tags"] or [])
    return out


def set_status(application_id: int, status: str, *, error: str | None = None,
               only_if_not_done: bool = False) -> bool:
    """Move an application to `status`. only_if_not_done: leave an
    application whose drafting finished (APPLICATION_DONE_STATUSES) alone.
    True if a row changed."""
    sql = ("UPDATE applications SET status = :status, error = :error, "
           "updated_at = now() WHERE id = :id")
    if only_if_not_done:
        sql += " AND status NOT IN :done"
    stmt = text(sql)
    params: dict[str, Any] = {"status": status, "error": error, "id": application_id}
    if only_if_not_done:
        stmt = stmt.bindparams(bindparam("done", expanding=True))
        params["done"] = list(APPLICATION_DONE_STATUSES)
    init_db()
    with get_engine().begin() as conn:
        return conn.execute(stmt, params).rowcount > 0


_LOCK_APPLICATION = text("""
    SELECT status, user_id, job_id, match_score, profile_hash, cache_key, version
    FROM applications WHERE id = :id FOR UPDATE
""")

_UPSERT_ARTIFACT = text("""
    INSERT INTO application_artifacts (application_id, kind, path, sha256, report)
    VALUES (:application_id, :kind, :path, :sha256, :report)
    ON CONFLICT (application_id, kind) DO UPDATE SET
        path = EXCLUDED.path, sha256 = EXCLUDED.sha256,
        report = EXCLUDED.report, created_at = now()
""").bindparams(bindparam("report", type_=JSONB(none_as_null=True)))

_SCORE_BREAKDOWN = text("""
    SELECT score_breakdown FROM job_scores
    WHERE job_id = :job_id AND profile_hash = :profile_hash AND cache_key = :cache_key
""")

_DRAFTED_EVENT = text("""
    INSERT INTO events (user_id, job_id, event_type, match_score, score_breakdown,
                        note, source)
    VALUES (:user_id, :job_id, 'drafted', :match_score, :score_breakdown, :note,
            'agent')
""").bindparams(bindparam("score_breakdown", type_=JSONB(none_as_null=True)))


class NotDraftable(RuntimeError):
    """persist() on an application that was already decided on."""


def persist(application_id: int, artifacts: dict[str, dict], *,
            needs_review: bool, version: int) -> bool:
    """One transaction: upsert every artifact by (application_id, kind),
    move the application to awaiting_approval at `version` with its
    needs_review flag, queue that version's approval expiry, and -- on the
    first draft only -- log ONE 'drafted' event with the score snapshot.
    The row is locked first. Setting (not incrementing) the version and
    keying the expiry on it make a retried persist (the worker died after
    this committed) change nothing more. Returns True if this call logged
    the event. NotDraftable once the application was decided on."""
    if version < 1:
        raise ValueError(f"a drafted version is >= 1, not {version!r}")
    init_db()
    with get_engine().begin() as conn:
        app = conn.execute(_LOCK_APPLICATION, {"id": application_id}).mappings().first()
        if app is None:
            raise ApplicationNotFound(f"application {application_id} does not exist")
        if app["status"] not in _DRAFTABLE:
            raise NotDraftable(f"application {application_id} is {app['status']}; "
                               "its documents can no longer change")
        first = app["status"] not in APPLICATION_DONE_STATUSES
        for kind, artifact in artifacts.items():
            conn.execute(_UPSERT_ARTIFACT, {
                "application_id": application_id, "kind": kind,
                "path": artifact["path"], "sha256": artifact["sha256"],
                "report": artifact.get("report")})
        conn.execute(text(
            "UPDATE applications SET status = 'awaiting_approval', error = NULL, "
            "needs_review = :needs_review, version = :version, updated_at = now() "
            "WHERE id = :id"),
            {"needs_review": needs_review, "version": version, "id": application_id})
        enqueue_expiry(application_id, version, conn=conn)
        if first:
            breakdown = conn.execute(_SCORE_BREAKDOWN, {
                "job_id": app["job_id"], "profile_hash": app["profile_hash"],
                "cache_key": app["cache_key"]}).scalar()
            conn.execute(_DRAFTED_EVENT, {
                "user_id": app["user_id"], "job_id": app["job_id"],
                "match_score": app["match_score"], "score_breakdown": breakdown,
                "note": (f"Agent drafted a tailored resume and cover letter "
                         f"(application #{application_id}"
                         f"{', needs review' if needs_review else ''}); "
                         "awaiting your approval, nothing was sent.")})
    return first


# --------------------------------------------------------------------------
# reads for the API
# --------------------------------------------------------------------------
_LIST = """
    SELECT a.id, a.user_id, a.job_id, a.status, a.match_score, a.profile_hash,
           a.cache_key, a.thread_id, a.error, a.needs_review, a.version,
           a.created_at, a.updated_at, j.title, j.company,
           COALESCE(a.posting->>'location', j.location) AS location,
           COALESCE(a.posting->>'apply_link', j.apply_link) AS apply_link
    FROM applications a JOIN canonical_jobs j ON j.id = a.job_id
"""


def _application(row) -> dict:
    out = dict(row)
    out["match_score"] = None if row["match_score"] is None else float(row["match_score"])
    return out


def list_applications(*, status: str | None = None, limit: int = 50,
                      offset: int = 0) -> list[dict]:
    """Newest first."""
    sql, params = _LIST, {"limit": limit, "offset": offset}
    if status is not None:
        sql += " WHERE a.status = :status"
        params["status"] = status
    sql += " ORDER BY a.created_at DESC, a.id DESC LIMIT :limit OFFSET :offset"
    init_db()
    with get_engine().connect() as conn:
        return [_application(r) for r in conn.execute(text(sql), params).mappings()]


def get_application(application_id: int) -> dict | None:
    init_db()
    with get_engine().connect() as conn:
        row = conn.execute(text(_LIST + " WHERE a.id = :id"),
                           {"id": application_id}).mappings().first()
    return _application(row) if row is not None else None


def get_artifacts(application_id: int) -> list[dict]:
    init_db()
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT kind, path, sha256, report, created_at "
            "FROM application_artifacts WHERE application_id = :id ORDER BY kind"),
            {"id": application_id}).mappings().all()
    return [dict(r) for r in rows]


def application_task(application_id: int) -> dict | None:
    from .. import taskqueue
    init_db()
    with get_engine().connect() as conn:
        task_id = conn.execute(text("SELECT id FROM tasks WHERE idempotency_key = :k"),
                               {"k": application_idempotency_key(application_id)}).scalar()
    return taskqueue.get_task(task_id) if task_id is not None else None
