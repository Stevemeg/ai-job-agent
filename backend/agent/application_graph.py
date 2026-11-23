"""
application_graph.py -- one application, from draft to the human's
decision: one LangGraph thread "application:<application_id>".

  load -> extract_requirements -> tailor_resume -> validate_resume
       -> cover_letter -> validate_letter -> render -> persist
       -> await_approval --approve--> execute -> email_draft -> END
                         --decline--> decline -> END
                         --expire---> expire -> END
                         --edit-----> apply_edit -> render -> persist
                                      -> await_approval (the next version)

Drafting (Phase 4), run by an "application" task:
- One LLM call per LLM node (extract_requirements, tailor_resume,
  cover_letter); every other node makes none. The JD requirements are
  extracted once and shared through the graph state by both documents.
- The validators are the resume engine's: a flagged resume project falls
  back to its original bullets, a flagged letter paragraph is reported.
  Nothing is regenerated. Any flag sets needs_review. Letters use the
  cover-letter rules (Phase 6, resume_engine/letter_validator.py); resume
  bullets the unchanged bullet validator.
- render writes data/applications/<application_id>/ (resume.docx,
  cover_letter.docx, cover_letter.txt, jd_requirements.json) -- never the
  shared DATA_DIR names the UI and CLIs use.
- persist upserts the artifacts by (application_id, kind), moves the
  application to awaiting_approval (at its version, needs_review = any
  flag), queues that version's approval expiry and, on the first draft,
  logs ONE 'drafted' event -- one transaction, idempotent.

The approval gate (Phase 5): await_approval calls interrupt({application_id,
version}). The thread pauses durably in the checkpointer and the
"application" task COMPLETES (result state "awaiting_approval"): no worker
waits for a person. A decision -- from the API/UI, or the approval TTL --
is an approvals row plus an "application_decision" task, whose handler
(run_decision_task) resumes the SAME thread with Command(resume=decision).
Nothing after await_approval calls the LLM:
- approve: execute logs 'approved', builds the packet (packet.json: the
  documents, apply link, job summary, why it matched, gaps) ->
  ready_to_apply; email_draft then creates ONE Gmail draft (never sent)
  if the JD names a contact email and Gmail is configured ->
  email_drafted. The agent never submits anything: the person does.
- decline / expire: 'declined' / 'expired' and a 'declined' event
  (source user / expiry).
- edit: the edited paragraphs/bullets are re-validated, re-rendered and
  persisted as version + 1, and the graph interrupts again (with a new
  expiry task for the new version).

Each handler takes the application's thread lock (checkpoint.thread_lock)
for the whole graph run, so a drafting task and a decision task never
drive one thread at once. A retried task resumes at the node its last
attempt died in.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, TypedDict

from ..config import DATA_DIR
from . import store

log = logging.getLogger(__name__)

APPLICATIONS_DIR = DATA_DIR / "applications"
ARTIFACT_FILES = {"resume_docx": "resume.docx",
                  "cover_letter_docx": "cover_letter.docx",
                  "cover_letter_txt": "cover_letter.txt",
                  "jd_requirements": "jd_requirements.json"}
PACKET_FILE = "packet.json"
# The provider's default model (Groq: llama-3.3-70b-versatile, Ollama:
# mistral), as for the UI and CLIs.
MODEL = None
DECISION_ACTIONS = ("approve", "decline", "edit", "expire")


class AppState(TypedDict, total=False):
    application_id: int
    user_id: int
    job_id: int
    job: dict
    profile: dict
    jd_requirements: Any
    tailored: dict
    resume_content: dict
    resume_report: list
    paragraphs: list
    letter_review: list
    artifacts: dict
    status: str
    needs_review: bool
    version: int
    decision: dict
    edit_flags: list
    packet: dict
    contact_email: Any
    email: dict


def application_dir(application_id: int) -> Path:
    return APPLICATIONS_DIR / str(application_id)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def needs_review(resume_report: list, letter_review: list) -> bool:
    return any(not r["ok"] for r in resume_report) or bool(letter_review)


def build_application_graph(ctx: Any = None):
    """The application graph's StateGraph builder; `ctx` is the worker's
    TaskContext (lease checks before writes), or None."""
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt
    from ..resume_engine import cover_letter, resume_tailor
    from ..services import ranking
    from . import decisions, packet

    def lease() -> None:
        if ctx is not None:
            ctx.ensure_lease()

    # ---- drafting ------------------------------------------------------------
    def load(state: AppState) -> dict:
        app = store.load_application(state["application_id"])
        profile = ranking._load_profile()
        lease()
        store.set_status(app["id"], "drafting", only_if_not_done=True)
        job = {k: app[k] for k in ("title", "company", "location", "apply_link",
                                   "clean_description", "tags", "seniority")}
        return {"user_id": app["user_id"], "job_id": app["job_id"], "job": job,
                "profile": profile}

    def extract_requirements(state: AppState) -> dict:
        jd = state["job"].get("clean_description") or ""
        return {"jd_requirements": resume_tailor.extract_jd_requirements(jd, MODEL)}

    def tailor_resume(state: AppState) -> dict:
        return {"tailored": resume_tailor.tailor_resume_single_call(
            state["profile"], state["jd_requirements"], MODEL)}

    def validate_resume(state: AppState) -> dict:
        content, report = resume_tailor.build_tailored_resume(
            state["profile"], state["job"], state["jd_requirements"], MODEL,
            tailored=state["tailored"], verbose=False)
        return {"resume_content": content, "resume_report": report}

    def write_cover_letter(state: AppState) -> dict:
        paragraphs = cover_letter.draft_cover_letter(
            state["profile"], state["job"], state["jd_requirements"], MODEL)
        if not paragraphs:
            raise RuntimeError("The model did not return usable cover-letter "
                               "paragraphs (LLM output is non-deterministic; "
                               "the task retries this node).")
        return {"paragraphs": paragraphs}

    def validate_letter(state: AppState) -> dict:
        return {"letter_review": cover_letter.letter_review(
            state["paragraphs"], state["profile"], state["job"], letter_rules=True)}

    def render(state: AppState) -> dict:
        lease()
        out = application_dir(state["application_id"])
        out.mkdir(parents=True, exist_ok=True)
        paths = {kind: out / name for kind, name in ARTIFACT_FILES.items()}
        paths["jd_requirements"].write_text(
            json.dumps(state["jd_requirements"], indent=2, ensure_ascii=False),
            encoding="utf-8")
        resume_tailor.render_resume_docx(state["resume_content"], paths["resume_docx"])
        try:
            cover_letter.render_cover_letter(state["paragraphs"],
                                             paths["cover_letter_docx"],
                                             state["profile"], state["job"])
        except FileNotFoundError:
            raise RuntimeError("Node.js not found -- .docx generation needs node "
                               "on PATH (the Docker image has it).") from None
        cover_letter.render_cover_letter(state["paragraphs"], paths["cover_letter_txt"],
                                         state["profile"], state["job"])
        return {"artifacts": {kind: {"path": str(p), "sha256": _sha256(p)}
                              for kind, p in paths.items()}}

    def persist(state: AppState) -> dict:
        resume_report, review = state["resume_report"], state["letter_review"]
        flagged = needs_review(resume_report, review)
        version = state.get("version") or 1        # apply_edit sets version + 1
        resume = {"ok": all(r["ok"] for r in resume_report), "projects": resume_report}
        letter = {"ok": not review, "paragraphs": review}
        reports = {"resume_docx": resume, "cover_letter_docx": letter,
                   "cover_letter_txt": letter, "jd_requirements": None}
        artifacts = {kind: {**a, "report": reports[kind]}
                     for kind, a in state["artifacts"].items()}
        lease()
        logged = store.persist(state["application_id"], artifacts,
                               needs_review=flagged, version=version)
        log.info("application %s: awaiting approval at version %d%s (%s)",
                 state["application_id"], version,
                 ", needs review" if flagged else "",
                 "drafted event logged" if logged else "already recorded")
        return {"status": "awaiting_approval", "needs_review": flagged,
                "version": version}

    # ---- the approval gate ------------------------------------------------------
    def await_approval(state: AppState) -> dict:
        # Re-runs from the top when resumed: nothing before interrupt().
        version = state.get("version") or 1
        decision = interrupt({"application_id": state["application_id"],
                              "version": version})
        if (not isinstance(decision, dict) or decision.get("action") not in DECISION_ACTIONS
                or decision.get("version") != version):
            raise ValueError(f"application {state['application_id']}: a decision for "
                             f"version {version} was expected, got {decision!r}")
        return {"decision": decision}

    def route(state: AppState) -> str:
        return {"approve": "execute", "decline": "decline", "expire": "expire",
                "edit": "apply_edit"}[state["decision"]["action"]]

    def apply_edit(state: AppState) -> dict:
        from .edits import EDIT_KEYS, apply_edits
        stored = state["decision"].get("edits") or {}     # + acknowledged_flags
        result = apply_edits(state, {k: v for k, v in stored.items() if k in EDIT_KEYS})
        if result.flags:
            log.warning("application %s: edit kept with validator flags (acknowledged): %s",
                        state["application_id"], result.flags)
        return {"paragraphs": result.paragraphs, "resume_content": result.resume_content,
                "resume_report": result.resume_report,
                "letter_review": result.letter_review, "edit_flags": result.flags,
                "version": (state.get("version") or 1) + 1}

    def execute(state: AppState) -> dict:
        decision, application_id = state["decision"], state["application_id"]
        lease()
        decisions.record_approved(application_id, decision)
        app = store.get_application(application_id)
        artifacts = {a["kind"]: a for a in store.get_artifacts(application_id)}
        approval = decisions.get_approval(decision["approval_id"])
        contact = packet.contact_email(state["job"].get("clean_description"))
        body = packet.build_packet(
            app=app, approval=approval, artifacts=artifacts,
            explanation=decisions.explanation(app).get("explanation"),
            jd_requirements=state.get("jd_requirements"), contact=contact)
        path = application_dir(application_id) / PACKET_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=2, ensure_ascii=False, default=str),
                        encoding="utf-8")
        sha = _sha256(path)
        lease()
        decisions.record_packet(application_id, str(path), sha)
        log.info("application %s approved: packet ready (%s)", application_id, path)
        return {"status": "ready_to_apply", "contact_email": contact,
                "packet": {"path": str(path), "sha256": sha}}

    def email_draft(state: AppState) -> dict:
        from ..integrations import gmail_auth
        application_id = state["application_id"]
        contact = state.get("contact_email")
        if not contact:
            return {"email": {"skipped": "the job description names no contact email"}}
        if not gmail_auth.configured():
            return {"email": {"skipped": "Gmail is not configured (GMAIL_CREDENTIALS_FILE)"}}
        from ..integrations import gmail
        version = state["decision"]["version"]
        message_id = packet.message_id_for(application_id, version)
        lease()
        action, created = decisions.begin_gmail_draft(application_id, message_id)
        if action["status"] == "done":
            return {"status": "email_drafted",
                    "email": {"draft_id": action["external_id"], "to": contact,
                              "message_id": action["message_id"]}}
        message_id = action["message_id"]          # the one recorded first
        draft_id = None
        if not created:          # an earlier attempt may have created it already
            draft_id = gmail.find_draft_by_message_id(message_id)
            if draft_id:
                log.info("application %s: found the Gmail draft %s from an earlier "
                         "attempt (Message-ID %s)", application_id, draft_id, message_id)
        if draft_id is None:
            artifacts = {a["kind"]: a for a in store.get_artifacts(application_id)}
            msg = packet.draft_message(
                app=store.get_application(application_id), version=version,
                profile=state["profile"], paragraphs=state["paragraphs"], to=contact,
                attachments=[Path(artifacts[k]["path"])
                             for k in ("resume_docx", "cover_letter_docx")])
            msg.replace_header("Message-ID", message_id)
            draft_id = gmail.create_draft(msg)
            log.info("application %s: Gmail draft %s created (never sent)",
                     application_id, draft_id)
        decisions.mark_gmail_draft_done(application_id, draft_id)
        return {"status": "email_drafted",
                "email": {"draft_id": draft_id, "to": contact, "message_id": message_id}}

    def decline(state: AppState) -> dict:
        lease()
        decisions.record_declined(state["application_id"], state["decision"])
        return {"status": "declined"}

    def expire(state: AppState) -> dict:
        lease()
        decisions.record_declined(state["application_id"], state["decision"])
        return {"status": "expired"}

    g = StateGraph(AppState)
    drafting = [("load", load), ("extract_requirements", extract_requirements),
                ("tailor_resume", tailor_resume), ("validate_resume", validate_resume),
                ("cover_letter", write_cover_letter), ("validate_letter", validate_letter),
                ("render", render), ("persist", persist),
                ("await_approval", await_approval)]
    for name, fn in drafting + [("apply_edit", apply_edit), ("execute", execute),
                                ("email_draft", email_draft), ("decline", decline),
                                ("expire", expire)]:
        g.add_node(name, fn)
    g.add_edge(START, drafting[0][0])
    for (a, _), (b, _) in zip(drafting, drafting[1:]):
        g.add_edge(a, b)
    g.add_conditional_edges("await_approval", route,
                            ["execute", "decline", "expire", "apply_edit"])
    g.add_edge("apply_edit", "render")
    g.add_edge("execute", "email_draft")
    for last in ("email_draft", "decline", "expire"):
        g.add_edge(last, END)
    return g


NODES = ("load", "extract_requirements", "tailor_resume", "validate_resume",
         "cover_letter", "validate_letter", "render", "persist", "await_approval",
         "apply_edit", "execute", "email_draft", "decline", "expire")


def _interrupt_versions(snapshot) -> list:
    return [i.value.get("version") for i in (snapshot.interrupts or ())
            if isinstance(i.value, dict)]


def _summary(application_id: int, final: dict, waiting: bool) -> dict:
    return {"application_id": application_id,
            "state": "awaiting_approval" if waiting else final.get("status"),
            "status": final.get("status"), "needs_review": final.get("needs_review"),
            "version": final.get("version"),
            "artifacts": {k: a["sha256"] for k, a in (final.get("artifacts") or {}).items()},
            "resume_flags": sum(not r["ok"] for r in final.get("resume_report") or []),
            "letter_flags": len(final.get("letter_review") or [])}


def run_application_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "application" tasks: draft, then stop at the
    approval gate. The task completes with state "awaiting_approval"."""
    from .checkpoint import run_thread, thread_lock
    application_id = int(payload["application_id"])
    with thread_lock(application_id):
        final = run_thread(build_application_graph(ctx),
                           store.application_thread(application_id),
                           {"application_id": application_id})
    return _summary(application_id, final, bool(final.get("__interrupt__")))


def _decision_from(approval: dict) -> dict:
    return {"action": approval["decision"], "version": approval["application_version"],
            "reason": approval["reason"], "edits": approval["edits"],
            "approval_id": approval["id"], "source": approval["source"]}


def _resume(application_id: int, decision: dict, ctx: Any) -> tuple[dict, str]:
    """Resume the thread with `decision`. Returns (final values, outcome):

      resumed          the gate was waiting at this version: Command(resume)
      continued        an earlier attempt resumed it and died mid-way: the
                       pending nodes run (invoke(None)), nothing is redone
      already_applied  the thread already moved past this version (a
                       retried edit whose next version is now waiting) or
                       finished: nothing runs
      resumed_legacy   a Phase 4 thread (ended after persist): moved onto
                       the gate, then resumed
    """
    from langgraph.types import Command
    from .checkpoint import DURABILITY, open_checkpointer, thread_config
    config = thread_config(store.application_thread(application_id))
    version = decision["version"]
    with open_checkpointer() as saver:
        graph = build_application_graph(ctx).compile(checkpointer=saver)
        outcome = None
        for _ in range(4):
            snap = graph.get_state(config)
            waiting = _interrupt_versions(snap)
            if waiting:
                if waiting[0] == version:
                    final = graph.invoke(Command(resume=decision), config,
                                         durability=DURABILITY)
                    return final, outcome or "resumed"
                if waiting[0] > version:
                    return dict(snap.values), outcome or "already_applied"
                raise RuntimeError(f"application {application_id}: the gate waits on "
                                   f"version {waiting[0]}, older than decision {version}")
            if snap.next:
                # Drive pending nodes to the gate (drafting died after persist)
                # or to the end (this decision was resumed, then the worker died).
                final = graph.invoke(None, config, durability=DURABILITY)
                outcome = outcome or "continued"
                if not final.get("__interrupt__"):
                    return final, outcome
                continue
            if saver.get_tuple(config) is None:
                raise RuntimeError(f"application {application_id} has no checkpoint")
            app = store.get_application(application_id)
            if (snap.values.get("decision") is None and app is not None
                    and app["status"] == "awaiting_approval" and app["version"] == version):
                # Phase 4 ended the thread after persist; put it at the gate.
                graph.update_state(config, {"version": app["version"],
                                            "needs_review": app["needs_review"],
                                            "status": "awaiting_approval"},
                                   as_node="persist")
                outcome = "resumed_legacy"
                continue
            return dict(snap.values), outcome or "already_applied"
    raise RuntimeError(f"application {application_id}: could not resume the thread")


def run_decision_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "application_decision" tasks: resume the
    application's thread with the recorded decision (no LLM call for
    approve / decline / expire; edit re-validates and re-renders)."""
    from . import decisions
    from .checkpoint import thread_lock
    application_id = int(payload["application_id"])
    approval = decisions.get_approval(int(payload["approval_id"]))
    if approval is None:
        return {"application_id": application_id, "approval_id": payload["approval_id"],
                "outcome": "approval_deleted"}
    decision = _decision_from(approval)
    with thread_lock(application_id):
        final, outcome = _resume(application_id, decision, ctx)
    app = store.get_application(application_id) or {}
    learned = None
    if decision["action"] in ("approve", "decline") and decision["source"] == "user" \
            and app.get("user_id") is not None:
        from . import memory              # Phase 6: learn from the decision (never raises)
        learned = memory.learn_safely(app["user_id"])
    result = {"application_id": application_id, "approval_id": approval["id"],
              "decision": decision["action"], "decided_version": decision["version"],
              "outcome": outcome, "status": app.get("status"),
              "version": app.get("version"),
              "state": "awaiting_approval" if final.get("__interrupt__") else "done"}
    if final.get("email"):
        result["email"] = final["email"]
    if learned is not None:
        result["learned"] = learned
    log.info("application %s: %s on version %d -> %s (%s)", application_id,
             decision["action"], decision["version"], result["status"], outcome)
    return result


def current_state(application_id: int) -> dict | None:
    """The application thread's checkpointed state (the documents as last
    drafted or edited), or None before its first checkpoint. Read-only."""
    from .checkpoint import open_checkpointer, thread_config
    config = thread_config(store.application_thread(application_id))
    with open_checkpointer() as saver:
        if saver.get_tuple(config) is None:
            return None
        snap = build_application_graph().compile(checkpointer=saver).get_state(config)
    return dict(snap.values)


def documents(state: dict | None) -> dict | None:
    """The reviewable text of a drafted application: the letter paragraphs
    and each resume project's bullets (from current_state())."""
    if not state or "paragraphs" not in state:
        return None
    content = state.get("resume_content") or {}
    return {"cover_letter_paragraphs": list(state["paragraphs"]),
            "resume_projects": [{"title": p.get("title"), "bullets": list(p.get("bullets") or [])}
                                for p in content.get("projects") or []],
            "skills": list(content.get("skills") or []),
            "edit_flags": list(state.get("edit_flags") or [])}


def run_expiry_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "approval_expiry" tasks: expire the application
    if nobody decided on that version in time (else a no-op)."""
    from . import decisions
    application_id, version = int(payload["application_id"]), int(payload["version"])
    if ctx is not None:
        ctx.ensure_lease()
    result = decisions.expire_if_unanswered(application_id, version)
    log.info("approval expiry for application %s version %d: %s", application_id,
             version, "expired" if result["expired"] else result["reason"])
    return {"application_id": application_id, "version": version, **result}


def on_application_dead(payload: dict, error: str | None) -> None:
    """Dead-task hook (the worker calls it once the task has no attempts
    left): the application is 'failed', with the last error's summary. A
    finished draft (APPLICATION_DONE_STATUSES) is never overwritten."""
    from ..services.task_status import error_summary
    application_id = int(payload["application_id"])
    changed = store.set_status(application_id, "failed",
                               error=error_summary(error) or "task died",
                               only_if_not_done=True)
    if changed:
        log.error("application %s failed: %s", application_id, error_summary(error))


def on_decision_dead(payload: dict, error: str | None) -> None:
    """Dead-task hook for "application_decision": pending actions are
    'failed' and the error is on the application. If the graph never
    consumed the decision (the gate still waits on its version), the
    approvals row is removed so the person can decide again."""
    from sqlalchemy import text
    from ..database.db import get_engine
    from ..services.task_status import error_summary
    from . import decisions
    application_id = int(payload["application_id"])
    summary = error_summary(error) or "task died"
    approval = decisions.get_approval(int(payload["approval_id"]))
    unconsumed = False
    if approval is not None:
        try:
            from .checkpoint import open_checkpointer, thread_config
            config = thread_config(store.application_thread(application_id))
            with open_checkpointer() as saver:
                snap = build_application_graph().compile(
                    checkpointer=saver).get_state(config)
            unconsumed = _interrupt_versions(snap)[:1] == [approval["application_version"]]
        except Exception:                                   # noqa: BLE001
            log.exception("application %s: can't read its thread", application_id)
    if unconsumed:
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval["id"]})
        summary = (f"Your {approval['decision']} could not be applied ({summary}); "
                   "decide again.")
    decisions.fail_pending_actions(application_id, summary)
    log.error("application %s: decision task dead: %s", application_id, summary)
