"""
decisions.py -- the approval gate's reads and writes (Phase 5).

How a decision travels:

  POST /v1/applications/{id}/decision  (or the Streamlit Inbox)
      submit_decision(): ONE transaction -- lock the application, check it
      is awaiting_approval at the version the person saw (else 409),
      insert the approvals row (UNIQUE per version: a second decision is
      a 409 too) and enqueue an "application_decision" task keyed
      "decision:<approval_id>".
  "approval_expiry" task (due AGENT_APPROVAL_TTL_DAYS after each draft)
      expire_if_unanswered(): the same transaction shape, decision
      'expire' / source 'expiry' -- but only if the application is still
      awaiting at that version with no decision; otherwise a no-op.
  "application_decision" task
      resumes the application's LangGraph thread with Command(resume=...)
      (application_graph.run_decision_task). Its nodes call the
      record_* functions below. None of them makes an LLM call.

Every status change is guarded by the status (and version) it expects,
so a retried node changes nothing twice, and each event is logged once.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from ..database.db import get_engine, init_db
from . import store

DECISION_KIND = "application_decision"
DECISION_MAX_ATTEMPTS = 3
USER_ACTIONS = ("approve", "decline", "edit")
EXECUTED = ("ready_to_apply", "email_drafted")


def decision_idempotency_key(approval_id: int) -> str:
    return f"decision:{approval_id}"


class DecisionError(Exception):
    """A decision that can't be taken; .status_code is the HTTP status."""
    status_code = 400

    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra


class ApplicationMissing(DecisionError):
    status_code = 404


class DecisionConflict(DecisionError):
    status_code = 409


class InvalidDecision(DecisionError):
    status_code = 422


class FlaggedEdit(DecisionError):
    """The edited text has validator flags and acknowledge_flags was not set."""
    status_code = 422

    def __init__(self, flags: list[dict]):
        super().__init__("The edit has unverified claims; fix them or resend with "
                         "acknowledge_flags=true to keep them (they stay flagged).",
                         flags=flags)
        self.flags = flags


# --------------------------------------------------------------------------
# submitting a decision (API / UI) and expiring one (approval_expiry task)
# --------------------------------------------------------------------------
_LOCK = text("""
    SELECT id, status, version, user_id, job_id, match_score, profile_hash, cache_key
    FROM applications WHERE id = :id FOR UPDATE
""")

_INSERT_APPROVAL = text("""
    INSERT INTO approvals (application_id, decision, edits, reason, source,
                           application_version)
    VALUES (:application_id, :decision, :edits, :reason, :source, :version)
    ON CONFLICT (application_id, application_version) DO NOTHING
    RETURNING id
""").bindparams(bindparam("edits", type_=JSONB(none_as_null=True)))


def _record(conn, application_id: int, *, decision: str, version: int, source: str,
            reason: str | None = None, edits: dict | None = None) -> dict | None:
    """Insert the approval and enqueue its decision task (caller's
    transaction). None if this version already has a decision."""
    from .. import taskqueue
    approval_id = conn.execute(_INSERT_APPROVAL, {
        "application_id": application_id, "decision": decision, "edits": edits,
        "reason": reason, "source": source, "version": version}).scalar()
    if approval_id is None:
        return None
    task_id = taskqueue.enqueue(
        DECISION_KIND, {"application_id": application_id, "approval_id": approval_id},
        idempotency_key=decision_idempotency_key(approval_id),
        max_attempts=DECISION_MAX_ATTEMPTS, conn=conn)
    return {"approval_id": approval_id, "task_id": task_id,
            "application_id": application_id, "decision": decision,
            "version": version, "source": source}


def submit_decision(application_id: int, *, action: str, version: int,
                    reason: str | None = None, edits: dict | None = None,
                    acknowledge_flags: bool = False) -> dict:
    """A person's approve / decline / edit on `version`. Returns the
    approval and its task. Raises ApplicationMissing (404),
    DecisionConflict (409: not awaiting, or another version, or already
    decided), InvalidDecision (422) or FlaggedEdit (422)."""
    if action not in USER_ACTIONS:
        raise InvalidDecision(f"action must be one of {', '.join(USER_ACTIONS)}")
    reason = (reason or "").strip() or None
    stored_edits = None
    if action == "edit":
        stored_edits = _check_edits(application_id, version, edits, acknowledge_flags)
    elif edits:
        raise InvalidDecision("edits go with action 'edit' only")
    init_db()
    with get_engine().begin() as conn:
        app = conn.execute(_LOCK, {"id": application_id}).mappings().first()
        if app is None:
            raise ApplicationMissing(f"No application {application_id}.")
        if app["status"] != "awaiting_approval":
            raise DecisionConflict(f"Application {application_id} is {app['status']}, "
                                   "not awaiting approval.", status=app["status"],
                                   version=app["version"])
        if app["version"] != version:
            raise DecisionConflict(f"Application {application_id} is at version "
                                   f"{app['version']}, not {version}; reload it.",
                                   status=app["status"], version=app["version"])
        made = _record(conn, application_id, decision=action, version=version,
                       source="user", reason=reason, edits=stored_edits)
        if made is None:
            raise DecisionConflict(f"Version {version} of application {application_id} "
                                   "already has a decision.", status=app["status"],
                                   version=app["version"])
    return made


def _check_edits(application_id: int, version: int, edits, acknowledge: bool) -> dict:
    """Validate an edit against the checkpointed documents it changes (no
    LLM): InvalidDecision if malformed, FlaggedEdit if flagged and not
    acknowledged. Returns what the approvals row stores."""
    from . import application_graph
    from .edits import InvalidEdit, apply_edits, normalize
    try:
        clean = normalize(edits)
    except InvalidEdit as exc:
        raise InvalidDecision(str(exc)) from None
    state = application_graph.current_state(application_id)
    if state is None:
        raise ApplicationMissing(f"Application {application_id} has no drafted documents.")
    if (state.get("version") or 1) != version:
        raise DecisionConflict(f"Application {application_id} is at version "
                               f"{state.get('version') or 1}, not {version}; reload it.")
    try:
        result = apply_edits(state, clean)
    except InvalidEdit as exc:
        raise InvalidDecision(str(exc)) from None
    if result.flags and not acknowledge:
        raise FlaggedEdit(result.flags)
    return {**clean, "acknowledged_flags": result.flags}


def expire_if_unanswered(application_id: int, version: int) -> dict:
    """The approval_expiry task: record 'expire' (source 'expiry') and
    queue its decision task -- only if the application is still awaiting
    approval at `version` with no decision on it. Otherwise a no-op that
    says why."""
    init_db()
    with get_engine().begin() as conn:
        app = conn.execute(_LOCK, {"id": application_id}).mappings().first()
        if app is None:
            return {"expired": False, "reason": "application deleted"}
        if app["status"] != "awaiting_approval" or app["version"] != version:
            return {"expired": False,
                    "reason": f"application is {app['status']} at version "
                              f"{app['version']}, not awaiting version {version}"}
        made = _record(conn, application_id, decision="expire", version=version,
                       source="expiry",
                       reason=f"No decision within {store._approval_ttl_days()} days.")
        if made is None:
            return {"expired": False, "reason": f"version {version} already has a decision"}
    return {"expired": True, **made}


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
def get_approval(approval_id: int) -> dict | None:
    init_db()
    with get_engine().connect() as conn:
        row = conn.execute(text("SELECT * FROM approvals WHERE id = :id"),
                           {"id": approval_id}).mappings().first()
    return dict(row) if row is not None else None


def list_approvals(application_id: int) -> list[dict]:
    init_db()
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT id, decision, edits, reason, source, application_version, decided_at "
            "FROM approvals WHERE application_id = :id ORDER BY application_version, id"),
            {"id": application_id}).mappings().all()
    return [dict(r) for r in rows]


def list_actions(application_id: int) -> list[dict]:
    init_db()
    with get_engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT action, status, external_id, message_id, created_at, updated_at "
            "FROM application_actions WHERE application_id = :id ORDER BY id"),
            {"id": application_id}).mappings().all()
    return [dict(r) for r in rows]


def explanation(app: dict) -> dict:
    """The matcher's score breakdown and explanation for this application's
    score (why it matched, the gaps), or {}."""
    init_db()
    with get_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT score_breakdown, explanation FROM job_scores WHERE job_id = :j "
            "AND profile_hash = :p AND cache_key = :k"),
            {"j": app["job_id"], "p": app["profile_hash"],
             "k": app["cache_key"]}).mappings().first()
    return dict(row) if row is not None else {}


# --------------------------------------------------------------------------
# the graph's writes after a decision
# --------------------------------------------------------------------------
_EVENT = text("""
    INSERT INTO events (user_id, job_id, event_type, match_score, score_breakdown,
                        note, source)
    VALUES (:user_id, :job_id, :event_type, :match_score, :score_breakdown, :note,
            :source)
""").bindparams(bindparam("score_breakdown", type_=JSONB(none_as_null=True)))


def _log_event(conn, app, event_type: str, note: str, source: str) -> None:
    breakdown = conn.execute(text(
        "SELECT score_breakdown FROM job_scores WHERE job_id = :j AND "
        "profile_hash = :p AND cache_key = :k"),
        {"j": app["job_id"], "p": app["profile_hash"], "k": app["cache_key"]}).scalar()
    conn.execute(_EVENT, {"user_id": app["user_id"], "job_id": app["job_id"],
                          "event_type": event_type, "match_score": app["match_score"],
                          "score_breakdown": breakdown, "note": note, "source": source})


def _transition(application_id: int, version: int, *, to: str, done: tuple[str, ...],
                event: tuple[str, str, str] | None) -> bool:
    """awaiting_approval@version -> `to`, logging `event` (type, note,
    source) in the same transaction. False (and nothing written) if it
    already happened (status in `done`). Anything else is a bug."""
    init_db()
    with get_engine().begin() as conn:
        app = conn.execute(_LOCK, {"id": application_id}).mappings().first()
        if app is None:
            raise store.ApplicationNotFound(f"application {application_id} does not exist")
        if app["status"] in done and app["version"] == version:
            return False
        if app["status"] != "awaiting_approval" or app["version"] != version:
            raise RuntimeError(f"application {application_id} is {app['status']} at "
                               f"version {app['version']}; can't make it {to} "
                               f"for version {version}")
        conn.execute(text("UPDATE applications SET status = :s, error = NULL, "
                          "updated_at = now() WHERE id = :id"),
                     {"s": to, "id": application_id})
        if event is not None:
            _log_event(conn, app, *event)
    return True


def record_approved(application_id: int, decision: dict) -> bool:
    note = (f"Approved application #{application_id} (version {decision['version']}); "
            "the packet is ready -- nothing was sent or submitted."
            + (f" Reason: {decision['reason']}" if decision.get("reason") else ""))
    return _transition(application_id, decision["version"], to="approved",
                       done=("approved",) + EXECUTED,
                       event=("approved", note, decision.get("source") or "user"))


def record_declined(application_id: int, decision: dict) -> bool:
    expired = decision["action"] == "expire"
    if expired:
        note = (f"Application #{application_id} (version {decision['version']}) "
                f"expired: {decision.get('reason') or 'no decision in time.'}")
    else:
        note = (f"Declined application #{application_id} (version {decision['version']})"
                + (f": {decision['reason']}" if decision.get("reason") else "."))
    return _transition(application_id, decision["version"],
                       to="expired" if expired else "declined",
                       done=("expired",) if expired else ("declined",),
                       event=("declined", note, "expiry" if expired else
                              decision.get("source") or "user"))


_UPSERT_PACKET = text("""
    INSERT INTO application_artifacts (application_id, kind, path, sha256, report)
    VALUES (:id, 'packet', :path, :sha256, NULL)
    ON CONFLICT (application_id, kind) DO UPDATE SET
        path = EXCLUDED.path, sha256 = EXCLUDED.sha256, created_at = now()
""")

_UPSERT_ACTION = text("""
    INSERT INTO application_actions (application_id, action, status, external_id,
                                     message_id)
    VALUES (:id, :action, :status, :external_id, :message_id)
    ON CONFLICT (application_id, action) DO UPDATE SET
        status = EXCLUDED.status, external_id = EXCLUDED.external_id,
        message_id = EXCLUDED.message_id, updated_at = now()
""")


def record_packet(application_id: int, path: str, sha256: str) -> None:
    """The packet artifact, its 'packet' action (done), and approved ->
    ready_to_apply, in one transaction (idempotent)."""
    init_db()
    with get_engine().begin() as conn:
        conn.execute(_UPSERT_PACKET, {"id": application_id, "path": path,
                                      "sha256": sha256})
        conn.execute(_UPSERT_ACTION, {"id": application_id, "action": "packet",
                                      "status": "done", "external_id": None,
                                      "message_id": None})
        conn.execute(text("UPDATE applications SET status = 'ready_to_apply', "
                          "updated_at = now() WHERE id = :id AND status = 'approved'"),
                     {"id": application_id})


def begin_gmail_draft(application_id: int, message_id: str) -> tuple[dict, bool]:
    """The gmail_draft action row, inserted 'pending' with its Message-ID
    BEFORE Gmail is called. Returns (row, created): created=False means an
    earlier attempt got this far, and Gmail must be searched for that
    Message-ID before creating anything."""
    init_db()
    with get_engine().begin() as conn:
        created = conn.execute(text(
            "INSERT INTO application_actions (application_id, action, status, message_id) "
            "VALUES (:id, 'gmail_draft', 'pending', :mid) "
            "ON CONFLICT (application_id, action) DO NOTHING RETURNING id"),
            {"id": application_id, "mid": message_id}).scalar() is not None
        row = conn.execute(text(
            "SELECT status, external_id, message_id FROM application_actions "
            "WHERE application_id = :id AND action = 'gmail_draft'"),
            {"id": application_id}).mappings().one()
    return dict(row), created


def mark_gmail_draft_done(application_id: int, draft_id: str) -> None:
    """The draft exists: its row done with the draft id, and the
    application ready_to_apply -> email_drafted (one transaction)."""
    init_db()
    with get_engine().begin() as conn:
        conn.execute(text(
            "UPDATE application_actions SET status = 'done', external_id = :d, "
            "updated_at = now() WHERE application_id = :id AND action = 'gmail_draft'"),
            {"id": application_id, "d": draft_id})
        conn.execute(text(
            "UPDATE applications SET status = 'email_drafted', error = NULL, "
            "updated_at = now() WHERE id = :id AND status = 'ready_to_apply'"),
            {"id": application_id})


def fail_pending_actions(application_id: int, error: str) -> None:
    """A decision task died: pending actions are 'failed', and the error is
    on the application (its status is left as it got)."""
    init_db()
    with get_engine().begin() as conn:
        conn.execute(text(
            "UPDATE application_actions SET status = 'failed', updated_at = now() "
            "WHERE application_id = :id AND status = 'pending'"), {"id": application_id})
        conn.execute(text("UPDATE applications SET error = :e, updated_at = now() "
                          "WHERE id = :id"), {"id": application_id, "e": error})
