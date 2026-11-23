"""
followups.py -- follow up on an application you said you submitted
(Phase 6). Nothing here sends anything; the most it does is ONE Gmail
draft, which you send yourself if you want to.

  'applied' event logged (Tracker, POST /v1/tracker/events, the CLI) for a
  job the agent has an application for
      events.log_outcome() enqueues a "follow_up" task in the SAME
      transaction: run_after = the event's time + AGENT_FOLLOW_UP_DAYS (7),
      idempotency key "follow_up:<application_id>" (once per application,
      however often 'applied' is logged).

  the "follow_up" task, when due (run_follow_up_task)
      - a later outcome event for the job (interview, rejected, offer,
        dismiss): nothing to do.
      - otherwise: applications.follow_up_due = true (the Inbox lists it),
        and -- only if the JD names a contact address AND Gmail is
        configured -- ONE follow-up Gmail draft, exactly once: its
        application_actions row ('gmail_follow_up', pending, with the
        deterministic Message-ID "<aja-followup-<id>@ai-job-agent>") is
        committed before Gmail is called, and a retry searches Gmail for
        that Message-ID before creating anything (the Phase 5 pattern).

  logging a later outcome clears follow_up_due again.

The draft is a fixed template (job title, company, your name and contact
lines; no generated text) and still goes through the cover-letter
validator (ADR-001 guardrail 5): a flagged template is not drafted.
"""
from __future__ import annotations

import logging
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from sqlalchemy import bindparam, text

from ..database.db import get_engine, init_db
from . import store

log = logging.getLogger(__name__)

FOLLOW_UP_KIND = "follow_up"
FOLLOW_UP_MAX_ATTEMPTS = 3
FOLLOW_UP_ACTION = "gmail_follow_up"
# Events after 'applied' that mean there's nothing to chase.
LATER_OUTCOMES = ("interview", "rejected", "offer", "dismiss")


def follow_up_idempotency_key(application_id: int) -> str:
    return f"follow_up:{application_id}"


def message_id_for(application_id: int) -> str:
    return f"<aja-followup-{application_id}@ai-job-agent>"


def _follow_up_days() -> int:
    from .settings import AgentSettings, settings_from_env
    try:
        return settings_from_env().follow_up_days
    except ValueError:                 # a malformed value never blocks logging an event
        return AgentSettings().follow_up_days


# --------------------------------------------------------------------------
# scheduling: called by events.log_outcome() inside its transaction
# --------------------------------------------------------------------------
def on_event_logged(conn: Any, *, user_id: int, job_id: int, event_id: int,
                    event_type: str) -> list[int]:
    """The follow-up side of a logged event, in the caller's transaction.
    'applied': queue each of the job's applications' follow-up (returns the
    task ids). A later outcome: clear follow_up_due. Anything else: no-op."""
    if event_type in LATER_OUTCOMES:
        conn.execute(text(
            "UPDATE applications SET follow_up_due = false, updated_at = now() "
            "WHERE user_id = :u AND job_id = :j AND follow_up_due"),
            {"u": user_id, "j": job_id})
        return []
    if event_type != "applied":
        return []
    from .. import taskqueue
    application_ids = conn.execute(text(
        "SELECT id FROM applications WHERE user_id = :u AND job_id = :j ORDER BY id"),
        {"u": user_id, "j": job_id}).scalars().all()
    if not application_ids:
        return []
    due = conn.execute(text(
        "SELECT created_at + make_interval(days => :d) FROM events WHERE id = :e"),
        {"d": _follow_up_days(), "e": event_id}).scalar()
    task_ids = []
    for application_id in application_ids:
        task_ids.append(taskqueue.enqueue(
            FOLLOW_UP_KIND, {"application_id": application_id, "applied_event_id": event_id},
            idempotency_key=follow_up_idempotency_key(application_id), run_after=due,
            max_attempts=FOLLOW_UP_MAX_ATTEMPTS, conn=conn))
    log.info("follow-up for application(s) %s queued for %s", application_ids, due)
    return task_ids


# --------------------------------------------------------------------------
# the follow-up message (a fixed template, validated)
# --------------------------------------------------------------------------
def paragraphs_for(app: dict) -> list[str]:
    return [f"I recently applied for the {app['title']} role at {app['company']} and "
            "wanted to follow up on my application.",
            "I remain very interested in the role and would be glad to share anything "
            "else that would help your review.",
            "Thank you for your time and consideration."]


def follow_up_message(*, app: dict, profile: dict, to: str,
                      in_reply_to: str | None = None) -> EmailMessage:
    lines = ["Dear Hiring Team,", ""]
    for p in paragraphs_for(app):
        lines += [p, ""]
    lines += ["Sincerely,", profile.get("name", "")]
    lines += [v for v in (profile.get("email"), profile.get("phone")) if v]
    msg = EmailMessage()
    msg["To"] = to
    if profile.get("email"):
        msg["From"] = formataddr((profile.get("name") or "", profile["email"]))
    msg["Subject"] = (f"Following up: {app['title']} application -- "
                      f"{profile.get('name', '')}").strip(" -")
    msg["Message-ID"] = message_id_for(app["id"])
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content("\n".join(lines) + "\n")
    return msg


# --------------------------------------------------------------------------
# the task
# --------------------------------------------------------------------------
_LATER = text("""
    SELECT event_type FROM events
    WHERE user_id = :u AND job_id = :j AND id > :e AND event_type IN :later
    ORDER BY id LIMIT 1
""").bindparams(bindparam("later", expanding=True))


def begin_follow_up_draft(application_id: int, message_id: str) -> tuple[dict, bool]:
    """The gmail_follow_up action row, inserted 'pending' with its
    Message-ID BEFORE Gmail is called. created=False: an earlier attempt
    got this far, so search Gmail before creating anything."""
    init_db()
    with get_engine().begin() as conn:
        created = conn.execute(text(
            "INSERT INTO application_actions (application_id, action, status, message_id) "
            "VALUES (:id, :a, 'pending', :mid) "
            "ON CONFLICT (application_id, action) DO NOTHING RETURNING id"),
            {"id": application_id, "a": FOLLOW_UP_ACTION, "mid": message_id}).scalar() \
            is not None
        row = conn.execute(text(
            "SELECT status, external_id, message_id FROM application_actions "
            "WHERE application_id = :id AND action = :a"),
            {"id": application_id, "a": FOLLOW_UP_ACTION}).mappings().one()
    return dict(row), created


def mark_follow_up_draft_done(application_id: int, draft_id: str) -> None:
    init_db()
    with get_engine().begin() as conn:
        conn.execute(text(
            "UPDATE application_actions SET status = 'done', external_id = :d, "
            "updated_at = now() WHERE application_id = :id AND action = :a"),
            {"id": application_id, "a": FOLLOW_UP_ACTION, "d": draft_id})


def _email_draft(app: dict, profile: dict, description: str | None, ctx: Any) -> dict:
    from ..integrations import gmail_auth
    from ..resume_engine import cover_letter
    from . import decisions, packet
    contact = packet.contact_email(description)
    if not contact:
        return {"skipped": "the job description names no contact email"}
    if not gmail_auth.configured():
        return {"skipped": "Gmail is not configured (GMAIL_CREDENTIALS_FILE)"}
    review = cover_letter.letter_review(paragraphs_for(app), profile,
                                        {"title": app["title"], "company": app["company"]})
    if review:
        return {"skipped": f"the follow-up text has validator flags: {review}"}
    from ..integrations import gmail
    if ctx is not None:
        ctx.ensure_lease()
    action, created = begin_follow_up_draft(app["id"], message_id_for(app["id"]))
    if action["status"] == "done":
        return {"draft_id": action["external_id"], "to": contact,
                "message_id": action["message_id"]}
    message_id = action["message_id"]
    draft_id = None if created else gmail.find_draft_by_message_id(message_id)
    if draft_id:
        log.info("application %s: found the follow-up draft %s from an earlier attempt",
                 app["id"], draft_id)
    else:
        original = next((a["message_id"] for a in decisions.list_actions(app["id"])
                         if a["action"] == "gmail_draft" and a["message_id"]), None)
        msg = follow_up_message(app=app, profile=profile, to=contact, in_reply_to=original)
        msg.replace_header("Message-ID", message_id)
        draft_id = gmail.create_draft(msg)
        log.info("application %s: follow-up Gmail draft %s created (never sent)",
                 app["id"], draft_id)
    mark_follow_up_draft_done(app["id"], draft_id)
    return {"draft_id": draft_id, "to": contact, "message_id": message_id}


def run_follow_up_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "follow_up" tasks (see the module docstring)."""
    from ..services import ranking
    application_id = int(payload["application_id"])
    event_id = int(payload["applied_event_id"])
    app = store.get_application(application_id)
    base = {"application_id": application_id, "applied_event_id": event_id}
    if app is None:
        return {**base, "follow_up_due": False, "skipped": "application deleted"}
    init_db()
    with get_engine().connect() as conn:
        later = conn.execute(_LATER, {"u": app["user_id"], "j": app["job_id"], "e": event_id,
                                      "later": list(LATER_OUTCOMES)}).scalar()
        description = conn.execute(text(
            "SELECT clean_description FROM canonical_jobs WHERE id = :j"),
            {"j": app["job_id"]}).scalar()
    if later is not None:
        log.info("application %s: no follow-up, '%s' was logged after applying",
                 application_id, later)
        return {**base, "follow_up_due": False,
                "skipped": f"a later outcome was logged ({later})"}
    if ctx is not None:
        ctx.ensure_lease()
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE applications SET follow_up_due = true, updated_at = now() "
                          "WHERE id = :id"), {"id": application_id})
    email = _email_draft(app, ranking._load_profile(), description, ctx)
    log.info("application %s: follow-up due (%s)", application_id,
             f"draft {email['draft_id']}" if "draft_id" in email else email["skipped"])
    return {**base, "follow_up_due": True, "email": email}


def on_follow_up_dead(payload: dict, error: str | None) -> None:
    """Dead-task hook: a pending follow-up draft action is 'failed' and the
    error is on the application."""
    from ..services.task_status import error_summary
    from . import decisions
    decisions.fail_pending_actions(int(payload["application_id"]),
                                   f"Follow-up failed: {error_summary(error) or 'task died'}")


def list_follow_ups_due(user_id: int | None = None) -> list[dict]:
    """Applications whose follow-up is due, oldest first, with their
    follow-up draft (if any)."""
    sql = store._LIST + " WHERE a.follow_up_due"
    params: dict = {}
    if user_id is not None:
        sql += " AND a.user_id = :u"
        params["u"] = user_id
    sql += " ORDER BY a.updated_at, a.id"
    init_db()
    with get_engine().connect() as conn:
        rows = [store._application(r) for r in conn.execute(text(sql), params).mappings()]
        for row in rows:
            row["follow_up_draft"] = conn.execute(text(
                "SELECT status, external_id, message_id FROM application_actions "
                "WHERE application_id = :id AND action = :a"),
                {"id": row["id"], "a": FOLLOW_UP_ACTION}).mappings().first()
            if row["follow_up_draft"] is not None:
                row["follow_up_draft"] = dict(row["follow_up_draft"])
    return rows
