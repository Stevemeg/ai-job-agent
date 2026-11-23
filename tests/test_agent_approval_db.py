"""The approval gate against a real Postgres, in process: the application
task completes at the interrupt, and approve / decline / edit / expire
resume the same thread from its checkpoint -- with zero LLM calls for
approve, decline and expire. Gmail is the fake HTTP layer in
tests/_fake_gmail.py (a /send URL fails the test). The real SIGKILL of a
decision worker mid-Gmail is in test_agent_gmail_kill.py."""
import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.agent import (application_graph, checkpoint, decisions,  # noqa: E402
                           run_graph, store)
from backend.database import corpus               # noqa: E402
from backend.worker import Worker                 # noqa: E402
from _fake_gmail import FakeGmailSession    # noqa: E402

pytestmark = pytest.mark.db

JD = ("Build RAG pipelines with FAISS and LLM tooling for clinical Q&A. "
      "Python, Pandas and Git required.")
JD_WITH_EMAIL = JD + (" Accommodations: accommodations@testco.example. "
                      "Send your resume to careers@testco.example.")


@pytest.fixture
def gmail(agent_env, monkeypatch, tmp_path):
    """Gmail configured, over the fake HTTP layer."""
    from backend.integrations import gmail_auth
    fake = FakeGmailSession(tmp_path / "mailbox.json",
                            on_send=lambda url: pytest.fail(f"Gmail send called: {url}"))
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(tmp_path / "client.json"))
    monkeypatch.setattr(gmail_auth, "authorized_session", lambda: fake)
    return fake


def _draft(env, monkeypatch, jd=JD):
    corpus.upsert_jobs([{"title": "AI Engineer", "company": "TestCo",
                         "location": "Bengaluru, India", "tags": [], "seniority": "Junior",
                         "experience": "1 years", "clean_description": jd,
                         "apply_link": "https://x/apply", "source": "ats"}])
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    run_graph.request_agent_run(trigger="api")
    assert Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                  lease_s=30).run_once().outcome == "succeeded"
    run = Worker({"application": application_graph.run_application_task},
                 lease_s=30).run_once()
    assert run.outcome == "succeeded", run.error
    [app] = store.list_applications()
    env.llm.calls.clear()                       # count the decision side only
    return app["id"], run


def _decision_worker():
    return Worker({decisions.DECISION_KIND: application_graph.run_decision_task,
                   store.APPROVAL_EXPIRY_KIND: application_graph.run_expiry_task},
                  lease_s=30)


def _decide(app_id, action, version=1, **kw):
    made = decisions.submit_decision(app_id, action=action, version=version, **kw)
    run = _decision_worker().run_once()
    assert run is not None and run.outcome == "succeeded", run and run.error
    return made, taskqueue.get_task(run.task_id)


def _events(pg, event_type=None):
    sql = "SELECT event_type, note, source, match_score FROM events"
    if event_type:
        sql += " WHERE event_type = :t"
    with pg.connect() as conn:
        return conn.execute(text(sql + " ORDER BY id"), {"t": event_type}).all()


def _snapshot(app_id):
    with checkpoint.open_checkpointer() as saver:
        graph = application_graph.build_application_graph().compile(checkpointer=saver)
        return graph.get_state(checkpoint.thread_config(store.application_thread(app_id)))


def _make_expiries_due(pg):
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now() "
                          "WHERE kind = 'approval_expiry' AND status = 'queued'"))


# ---- the interrupt ------------------------------------------------------------------

def test_the_application_task_completes_at_the_gate(agent_env, monkeypatch):
    app_id, run = _draft(agent_env, monkeypatch)
    task = taskqueue.get_task(run.task_id)
    assert task["status"] == "succeeded" and task["result"]["state"] == "awaiting_approval"
    assert task["lease_expires_at"] is None                  # no worker holds it
    with agent_env.pg.connect() as conn:
        running = conn.execute(text(
            "SELECT count(*) FROM tasks WHERE status = 'running'")).scalar()
    assert running == 0
    snap = _snapshot(app_id)
    assert snap.next == ("await_approval",)
    assert [i.value for i in snap.interrupts] == [{"application_id": app_id, "version": 1}]
    app = store.get_application(app_id)
    assert (app["status"], app["version"]) == ("awaiting_approval", 1)
    # Running the task again while it waits: nothing re-runs.
    again = application_graph.run_application_task({"application_id": app_id})
    assert again["state"] == "awaiting_approval"
    assert sum(agent_env.llm.calls.values()) == 0


# ---- approve --------------------------------------------------------------------------

def test_approve_resumes_with_zero_llm_calls_and_builds_the_packet(agent_env, monkeypatch,
                                                                   gmail):
    app_id, _ = _draft(agent_env, monkeypatch)              # the JD has no email
    made, task = _decide(app_id, "approve", reason="Great fit")
    assert sum(agent_env.llm.calls.values()) == 0           # ZERO LLM calls
    assert task["result"]["outcome"] == "resumed"
    app = store.get_application(app_id)
    assert (app["status"], app["version"]) == ("ready_to_apply", 1)

    artifacts = {a["kind"]: a for a in store.get_artifacts(app_id)}
    path = Path(artifacts["packet"]["path"])
    assert path == agent_env.apps / str(app_id) / "packet.json"
    assert artifacts["packet"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["job"]["apply_link"] == "https://x/apply"
    assert body["job"]["title"] == "AI Engineer" and body["job"]["summary"]
    assert body["documents"]["resume_docx"]["sha256"] == artifacts["resume_docx"]["sha256"]
    assert body["approval_id"] == made["approval_id"] and body["contact_email"] is None
    assert "strong_matches" in body["why_match"] and isinstance(body["gaps"], list)

    assert [(a["action"], a["status"]) for a in decisions.list_actions(app_id)] == [
        ("packet", "done")]
    [approved] = _events(agent_env.pg, "approved")
    assert approved.source == "user" and "Great fit" in approved.note
    assert float(approved.match_score) == app["match_score"]
    assert gmail.requests() == []                           # no Gmail call: no email
    assert _snapshot(app_id).next == ()                     # the thread finished


def test_approve_with_a_contact_email_creates_one_gmail_draft(agent_env, monkeypatch, gmail):
    app_id, _ = _draft(agent_env, monkeypatch, jd=JD_WITH_EMAIL)
    _, task = _decide(app_id, "approve")
    assert sum(agent_env.llm.calls.values()) == 0
    app = store.get_application(app_id)
    assert app["status"] == "email_drafted"
    [draft] = gmail.drafts()
    assert draft["to"] == "careers@testco.example"         # not accommodations@
    assert draft["message_id"] == f"<aja-{app_id}-1@ai-job-agent>"
    assert [a["filename"] for a in draft["attachments"]] == ["resume.docx",
                                                             "cover_letter.docx"]
    assert all(a["size"] > 0 for a in draft["attachments"])
    letter = (agent_env.apps / str(app_id) / "cover_letter.txt").read_text(encoding="utf-8")
    assert "The candidate is applying to TestCo" in draft["body"] and \
        "The candidate is applying to TestCo" in letter
    actions = {a["action"]: a for a in decisions.list_actions(app_id)}
    assert actions["gmail_draft"]["status"] == "done"
    assert actions["gmail_draft"]["external_id"] == draft["id"]
    assert actions["gmail_draft"]["message_id"] == draft["message_id"]
    assert task["result"]["email"]["draft_id"] == draft["id"]
    assert gmail.send_attempts() == []
    assert not any("/send" in url for _, url, _ in gmail.requests())


def test_without_gmail_configured_an_email_jd_still_gets_its_packet(agent_env, monkeypatch):
    monkeypatch.delenv("GMAIL_CREDENTIALS_FILE", raising=False)
    app_id, _ = _draft(agent_env, monkeypatch, jd=JD_WITH_EMAIL)
    _, task = _decide(app_id, "approve")
    assert store.get_application(app_id)["status"] == "ready_to_apply"
    assert "not configured" in task["result"]["email"]["skipped"]
    assert [a["action"] for a in decisions.list_actions(app_id)] == ["packet"]


def test_double_approve_is_one_approval_and_a_conflict(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    decisions.submit_decision(app_id, action="approve", version=1)
    with pytest.raises(decisions.DecisionConflict, match="already has a decision"):
        decisions.submit_decision(app_id, action="approve", version=1)
    assert _decision_worker().run_once().outcome == "succeeded"
    with pytest.raises(decisions.DecisionConflict, match="not awaiting"):
        decisions.submit_decision(app_id, action="approve", version=1)
    assert len(decisions.list_approvals(app_id)) == 1
    assert len(_events(agent_env.pg, "approved")) == 1
    with agent_env.pg.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM tasks WHERE kind = "
                                 "'application_decision'")).scalar() == 1


def test_a_stale_version_is_a_conflict(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    with pytest.raises(decisions.DecisionConflict, match="version 1, not 2"):
        decisions.submit_decision(app_id, action="approve", version=2)
    with pytest.raises(decisions.ApplicationMissing):
        decisions.submit_decision(999, action="approve", version=1)
    with pytest.raises(decisions.InvalidDecision):
        decisions.submit_decision(app_id, action="send", version=1)
    assert decisions.list_approvals(app_id) == []


def test_a_retried_decision_continues_where_it_died(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    real = decisions.record_packet
    calls = {"n": 0}

    def dies_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk hiccup")
        return real(*args, **kwargs)
    monkeypatch.setattr(decisions, "record_packet", dies_once)
    decisions.submit_decision(app_id, action="approve", version=1)
    worker = _decision_worker()
    assert worker.run_once().outcome == "failed"
    assert store.get_application(app_id)["status"] == "approved"
    with agent_env.pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now() WHERE status = 'queued' "
                          "AND kind = 'application_decision'"))
    run = worker.run_once()
    task = taskqueue.get_task(run.task_id)
    assert run.outcome == "succeeded" and task["result"]["outcome"] == "continued"
    assert store.get_application(app_id)["status"] == "ready_to_apply"
    assert len(_events(agent_env.pg, "approved")) == 1      # logged once
    assert sum(agent_env.llm.calls.values()) == 0


# ---- decline ----------------------------------------------------------------------------

def test_decline_logs_the_reason_and_never_touches_gmail(agent_env, monkeypatch, gmail):
    app_id, _ = _draft(agent_env, monkeypatch, jd=JD_WITH_EMAIL)
    _decide(app_id, "decline", reason="Too far from home")
    assert sum(agent_env.llm.calls.values()) == 0
    assert store.get_application(app_id)["status"] == "declined"
    [declined] = _events(agent_env.pg, "declined")
    assert declined.source == "user" and "Too far from home" in declined.note
    assert gmail.requests() == []
    assert decisions.list_actions(app_id) == []
    assert "packet" not in {a["kind"] for a in store.get_artifacts(app_id)}
    # A declined job is never triaged again.
    run_graph.request_agent_run(trigger="api")
    assert Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                  lease_s=30).run_once().outcome == "succeeded"
    assert len(store.list_applications()) == 1


# ---- edit ----------------------------------------------------------------------------------

FLAGGED = "I led a Kubernetes team of 12 at Google."


def _paragraphs(app_id):
    return application_graph.current_state(app_id)["paragraphs"]


def test_a_flagged_edit_needs_acknowledging(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    new = _paragraphs(app_id)[:2] + [FLAGGED]
    with pytest.raises(decisions.FlaggedEdit) as exc:
        decisions.submit_decision(app_id, action="edit", version=1,
                                  edits={"cover_letter_paragraphs": new})
    assert exc.value.status_code == 422
    assert exc.value.flags[0]["paragraph"] == 3
    assert {"Kubernetes", "Google", "12"} <= set(exc.value.flags[0]["flags"])
    assert decisions.list_approvals(app_id) == []            # nothing recorded


def test_an_acknowledged_flagged_edit_is_redrafted_as_the_next_version(agent_env,
                                                                       monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    before = {a["kind"]: a["sha256"] for a in store.get_artifacts(app_id)}
    new = _paragraphs(app_id)[:2] + [FLAGGED]
    made, task = _decide(app_id, "edit", edits={"cover_letter_paragraphs": new},
                         acknowledge_flags=True)
    assert sum(agent_env.llm.calls.values()) == 0           # validators, not the LLM
    assert task["result"]["state"] == "awaiting_approval"
    assert (task["result"]["version"], task["result"]["status"]) == (2, "awaiting_approval")
    app = store.get_application(app_id)
    assert (app["status"], app["version"], app["needs_review"]) == (
        "awaiting_approval", 2, True)
    after = {a["kind"]: a for a in store.get_artifacts(app_id)}
    assert after["cover_letter_txt"]["sha256"] != before["cover_letter_txt"]
    assert after["cover_letter_docx"]["sha256"] != before["cover_letter_docx"]
    letter = (agent_env.apps / str(app_id) / "cover_letter.txt").read_text(encoding="utf-8")
    assert FLAGGED in letter                                 # kept as written
    assert after["cover_letter_txt"]["report"]["paragraphs"][0]["paragraph"] == 3
    [approval] = decisions.list_approvals(app_id)
    assert approval["edits"]["cover_letter_paragraphs"] == new
    assert approval["edits"]["acknowledged_flags"][0]["paragraph"] == 3
    snap = _snapshot(app_id)
    assert [i.value for i in snap.interrupts] == [{"application_id": app_id, "version": 2}]
    with agent_env.pg.connect() as conn:
        keys = conn.execute(text("SELECT idempotency_key FROM tasks WHERE kind = "
                                 "'approval_expiry' ORDER BY id")).scalars().all()
    assert keys == [f"expiry:{app_id}:1", f"expiry:{app_id}:2"]
    assert len(_events(agent_env.pg, "drafted")) == 1        # still one drafted event
    # The old version can't be decided any more; the new one can.
    with pytest.raises(decisions.DecisionConflict):
        decisions.submit_decision(app_id, action="approve", version=1)
    _decide(app_id, "approve", version=2)
    assert store.get_application(app_id)["status"] == "ready_to_apply"


def test_an_unflagged_edit_needs_no_acknowledgement(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    state = application_graph.current_state(app_id)
    project = state["resume_content"]["projects"][0]
    bullets = ["Deployed a Retrieval-Augmented Generation pipeline for clinical Q&A "
               "using FAISS and Sentence Transformers."]
    made = decisions.submit_decision(app_id, action="edit", version=1,
                                     edits={"resume_bullets": {project["title"]: bullets}})
    assert made["decision"] == "edit"
    assert _decision_worker().run_once().outcome == "succeeded"
    new_state = application_graph.current_state(app_id)
    assert new_state["resume_content"]["projects"][0]["bullets"] == bullets
    assert new_state["version"] == 2
    assert store.get_application(app_id)["needs_review"] is False


def test_a_retried_edit_task_does_not_apply_twice(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    new = _paragraphs(app_id)[:2]
    made, task = _decide(app_id, "edit", edits={"cover_letter_paragraphs": new})
    again = application_graph.run_decision_task(
        {"application_id": app_id, "approval_id": made["approval_id"]})
    assert again["outcome"] == "already_applied" and again["version"] == 2
    assert store.get_application(app_id)["version"] == 2


# ---- expiry ------------------------------------------------------------------------------------

def test_expiry_fires_expired_and_a_declined_event_from_expiry(agent_env, monkeypatch, gmail):
    app_id, _ = _draft(agent_env, monkeypatch)
    _make_expiries_due(agent_env.pg)
    worker = _decision_worker()
    expiry = worker.run_once()                    # the approval_expiry task
    assert taskqueue.get_task(expiry.task_id)["result"]["expired"] is True
    decision = worker.run_once()                  # its application_decision task
    assert decision.outcome == "succeeded", decision.error
    assert sum(agent_env.llm.calls.values()) == 0
    assert store.get_application(app_id)["status"] == "expired"
    [declined] = _events(agent_env.pg, "declined")
    assert declined.source == "expiry" and "expired" in declined.note
    [approval] = decisions.list_approvals(app_id)
    assert (approval["decision"], approval["source"]) == ("expire", "expiry")
    assert gmail.requests() == []


def test_expiry_for_an_old_version_is_a_no_op(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    _decide(app_id, "edit", edits={"cover_letter_paragraphs": _paragraphs(app_id)[:2]})
    result = application_graph.run_expiry_task({"application_id": app_id, "version": 1})
    assert result["expired"] is False and "version 2" in result["reason"]
    app = store.get_application(app_id)
    assert (app["status"], app["version"]) == ("awaiting_approval", 2)
    assert [a["decision"] for a in decisions.list_approvals(app_id)] == ["edit"]


def test_expiry_after_a_decision_is_a_no_op(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    decisions.submit_decision(app_id, action="decline", version=1)
    # The decision is recorded but not yet applied: the version is taken.
    pending = application_graph.run_expiry_task({"application_id": app_id, "version": 1})
    assert pending["expired"] is False and "already has a decision" in pending["reason"]
    assert _decision_worker().run_once().outcome == "succeeded"
    done = application_graph.run_expiry_task({"application_id": app_id, "version": 1})
    assert done["expired"] is False and "declined" in done["reason"]
    assert store.get_application(app_id)["status"] == "declined"
    assert [e.source for e in _events(agent_env.pg, "declined")] == ["user"]


# ---- a Phase 4 thread (it ended after persist) ---------------------------------------------

def test_a_phase4_thread_is_moved_onto_the_gate_and_approved(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    config = checkpoint.thread_config(store.application_thread(app_id))
    with checkpoint.open_checkpointer() as saver:
        graph = application_graph.build_application_graph().compile(checkpointer=saver)
        # Rewrite the thread to how Phase 4 left it: finished, no decision.
        graph.update_state(config, {"status": "drafted"}, as_node="decline")
        snap = graph.get_state(config)
    assert snap.next == () and not snap.interrupts and "decision" not in snap.values
    _, task = _decide(app_id, "approve")
    assert task["result"]["outcome"] == "resumed_legacy"
    assert store.get_application(app_id)["status"] == "ready_to_apply"
    assert sum(agent_env.llm.calls.values()) == 0


def test_a_dead_unconsumed_decision_can_be_made_again(agent_env, monkeypatch):
    app_id, _ = _draft(agent_env, monkeypatch)
    made = decisions.submit_decision(app_id, action="approve", version=1)
    application_graph.on_decision_dead(
        {"application_id": app_id, "approval_id": made["approval_id"]},
        "ThreadBusy: another task is running its graph")
    app = store.get_application(app_id)
    assert app["status"] == "awaiting_approval"
    assert app["error"].startswith("Your approve could not be applied")
    assert decisions.list_approvals(app_id) == []
    decisions.submit_decision(app_id, action="decline", version=1)   # allowed again


# ---- the tracker --------------------------------------------------------------------------------

def test_the_tracker_shows_approved_declined_and_expired(agent_env, monkeypatch, profile):
    from backend.database.tracker import get_funnel_counts, get_tracked_jobs
    corpus.upsert_jobs([{"title": title, "company": "TestCo", "location": "Pune",
                         "tags": [], "seniority": "Junior", "clean_description": JD,
                         "apply_link": f"https://x/{title}", "source": "ats"}
                        for title in ("AI Engineer", "ML Engineer", "LLM Engineer")])
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    run_graph.request_agent_run(trigger="api")
    worker = Worker(lease_s=30)
    while worker.run_once() is not None:
        pass
    apps = {a["title"]: a["id"] for a in store.list_applications()}
    assert len(apps) == 3
    decisions.submit_decision(apps["AI Engineer"], action="approve", version=1)
    decisions.submit_decision(apps["ML Engineer"], action="decline", version=1,
                              reason="Wrong stack")
    decisions.expire_if_unanswered(apps["LLM Engineer"], 1)
    while worker.run_once() is not None:
        pass

    tracked = {t["title"]: t for t in get_tracked_jobs(store.user_for_profile(profile))}
    assert {t: tracked[t]["status"] for t in tracked} == {
        "AI Engineer": "approved", "ML Engineer": "declined", "LLM Engineer": "expired"}
    history = {t: [(h["event_type"], h["source"]) for h in tracked[t]["history"]]
               for t in tracked}                                  # newest first
    assert history["AI Engineer"] == [("approved", "user"), ("drafted", "agent")]
    assert history["ML Engineer"] == [("declined", "user"), ("drafted", "agent")]
    assert history["LLM Engineer"] == [("declined", "expiry"), ("drafted", "agent")]
    assert "Wrong stack" in tracked["ML Engineer"]["history"][0]["note"]
    counts = get_funnel_counts(list(tracked.values()))
    assert (counts["approved"], counts["declined"], counts["expired"]) == (1, 1, 1)

