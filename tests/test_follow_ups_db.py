"""Follow-ups (backend/agent/followups.py) against a real Postgres: an
'applied' event schedules one follow_up task; a later interview /
rejection / offer / dismiss cancels it; when due it sets follow_up_due and,
with a contact email and Gmail configured, creates ONE follow-up draft
(Gmail is the fake HTTP layer; a /send URL fails the test). The real
SIGKILL after the Gmail create is in test_follow_up_kill.py."""
import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.agent import followups, store        # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.database.events import log_outcome   # noqa: E402
from backend.worker import Worker                 # noqa: E402
from _fake_gmail import FakeGmailSession          # noqa: E402

pytestmark = pytest.mark.db

JD = "Build RAG pipelines with FAISS. Questions? Write to careers@testco.example."


def _application(pg, title="AI Engineer", jd="Build RAG pipelines with FAISS.",
                 status="ready_to_apply", profile=None):
    corpus.upsert_jobs([{"title": title, "company": "TestCo", "location": "Pune",
                         "tags": [], "clean_description": jd, "apply_link": "https://x/a",
                         "source": "ats"}])
    with pg.connect() as conn:
        job_id = conn.execute(text("SELECT id FROM canonical_jobs WHERE title = :t"),
                              {"t": title}).scalar()
    user = store.user_for_profile(profile or {"email": "test@example.com"})
    made = store.dispatch(user_id=user, job_id=job_id, match_score=30, profile_hash="p",
                          cache_key="k")
    with pg.begin() as conn:
        conn.execute(text("UPDATE applications SET status = :s, version = 1 WHERE id = :a"),
                     {"s": status, "a": made["application_id"]})
    return user, made["application_id"]


def _follow_up_tasks(pg):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT t.id, t.payload, t.idempotency_key, t.status, "
            "round(extract(epoch FROM t.run_after - e.created_at)) AS delay_s "
            "FROM tasks t JOIN events e ON e.id = (t.payload->>'applied_event_id')::int "
            "WHERE t.kind = 'follow_up' ORDER BY t.id")).all()


def _make_due(pg):
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now() WHERE kind = 'follow_up'"))


def _run(pg):
    _make_due(pg)
    run = Worker({followups.FOLLOW_UP_KIND: followups.run_follow_up_task},
                 lease_s=30).run_once()
    assert run is not None and run.outcome == "succeeded", run and run.error
    return taskqueue.get_task(run.task_id)["result"]


@pytest.fixture
def gmail(agent_env, monkeypatch, tmp_path):
    from backend.integrations import gmail_auth
    fake = FakeGmailSession(tmp_path / "mailbox.json",
                            on_send=lambda url: pytest.fail(f"Gmail send called: {url}"))
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(tmp_path / "client.json"))
    monkeypatch.setattr(gmail_auth, "authorized_session", lambda: fake)
    return fake


# ---- scheduling ----------------------------------------------------------------------

def test_applied_schedules_one_follow_up_after_the_configured_days(agent_env, profile,
                                                                   monkeypatch):
    pg = agent_env.pg
    user, app_id = _application(pg, profile=profile)
    event_id = log_outcome(user, "TestCo", "AI Engineer", "applied")
    [task] = _follow_up_tasks(pg)
    assert task.payload == {"application_id": app_id, "applied_event_id": event_id}
    assert task.idempotency_key == f"follow_up:{app_id}"
    assert task.status == "queued" and task.delay_s == 7 * 86400   # AGENT_FOLLOW_UP_DAYS
    # Logged again (or from another screen): still one follow-up.
    log_outcome(user, "TestCo", "AI Engineer", "applied")
    assert len(_follow_up_tasks(pg)) == 1

    monkeypatch.setenv("AGENT_FOLLOW_UP_DAYS", "3")
    user, other = _application(pg, title="ML Engineer", profile=profile)
    log_outcome(user, "TestCo", "ML Engineer", "applied")
    assert [t.delay_s for t in _follow_up_tasks(pg)] == [7 * 86400, 3 * 86400]


def test_applied_for_a_job_without_an_application_schedules_nothing(agent_env, profile):
    user = store.user_for_profile(profile)
    log_outcome(user, "Elsewhere", "Data Engineer", "applied")
    log_outcome(user, "TestCo", "AI Engineer", "save")
    assert _follow_up_tasks(agent_env.pg) == []


def test_a_malformed_follow_up_setting_never_blocks_logging(agent_env, profile, monkeypatch):
    monkeypatch.setenv("AGENT_FOLLOW_UP_DAYS", "soon")
    user, _ = _application(agent_env.pg, profile=profile)
    assert log_outcome(user, "TestCo", "AI Engineer", "applied")
    assert [t.delay_s for t in _follow_up_tasks(agent_env.pg)] == [7 * 86400]


# ---- when it is due --------------------------------------------------------------------

def test_a_due_follow_up_with_no_reply_sets_the_flag(agent_env, profile):
    pg = agent_env.pg
    user, app_id = _application(pg, profile=profile)
    log_outcome(user, "TestCo", "AI Engineer", "applied")
    log_outcome(user, "TestCo", "AI Engineer", "save")            # not an outcome
    result = _run(pg)
    assert result["follow_up_due"] is True
    assert result["email"] == {"skipped": "the job description names no contact email"}
    assert store.get_application(app_id)["follow_up_due"] is True
    [due] = followups.list_follow_ups_due(user)
    assert (due["id"], due["follow_up_draft"]) == (app_id, None)
    # Logging the reply clears it.
    log_outcome(user, "TestCo", "AI Engineer", "interview")
    assert store.get_application(app_id)["follow_up_due"] is False
    assert followups.list_follow_ups_due(user) == []


@pytest.mark.parametrize("outcome", ["interview", "rejected", "offer", "dismiss"])
def test_a_later_outcome_cancels_the_follow_up(agent_env, profile, gmail, outcome):
    pg = agent_env.pg
    user, app_id = _application(pg, jd=JD, profile=profile)
    log_outcome(user, "TestCo", "AI Engineer", "applied")
    log_outcome(user, "TestCo", "AI Engineer", outcome)
    result = _run(pg)
    assert result["follow_up_due"] is False
    assert result["skipped"] == f"a later outcome was logged ({outcome})"
    assert store.get_application(app_id)["follow_up_due"] is False
    assert gmail.drafts() == [] and gmail.requests() == []          # Gmail untouched


def test_an_outcome_before_applying_does_not_cancel(agent_env, profile):
    pg = agent_env.pg
    user, _ = _application(pg, profile=profile)
    log_outcome(user, "TestCo", "AI Engineer", "dismiss")            # before 'applied'
    log_outcome(user, "TestCo", "AI Engineer", "applied")
    assert _run(pg)["follow_up_due"] is True


def test_with_gmail_one_follow_up_draft_is_created_once(agent_env, profile, gmail):
    pg = agent_env.pg
    user, app_id = _application(pg, jd=JD, profile=profile)
    with pg.begin() as conn:                     # the approval's own (earlier) draft
        conn.execute(text(
            "INSERT INTO application_actions (application_id, action, status, "
            "external_id, message_id) VALUES (:a, 'gmail_draft', 'done', 'r-0', :m)"),
            {"a": app_id, "m": f"<aja-{app_id}-1@ai-job-agent>"})
    log_outcome(user, "TestCo", "AI Engineer", "applied")
    task_payload = _follow_up_tasks(pg)[0].payload
    result = _run(pg)
    [draft] = gmail.drafts()
    assert result["email"] == {"draft_id": draft["id"], "to": "careers@testco.example",
                               "message_id": f"<aja-followup-{app_id}@ai-job-agent>"}
    headers = dict(draft["headers"])
    assert headers["Message-ID"] == f"<aja-followup-{app_id}@ai-job-agent>"
    assert headers["In-Reply-To"] == f"<aja-{app_id}-1@ai-job-agent>"
    assert draft["subject"] == f"Following up: AI Engineer application -- {profile['name']}"
    assert "I recently applied for the AI Engineer role at TestCo" in draft["body"]
    assert draft["attachments"] == []
    [action] = [a for a in _actions(app_id) if a["action"] == "gmail_follow_up"]
    assert (action["status"], action["external_id"]) == ("done", draft["id"])
    # The handler again (a retry after its row was done): no second draft.
    again = followups.run_follow_up_task(task_payload)
    assert again["email"]["draft_id"] == draft["id"]
    assert len(gmail.drafts()) == 1 and gmail.send_attempts() == []


def test_without_gmail_a_contact_email_only_sets_the_flag(agent_env, profile):
    user, app_id = _application(agent_env.pg, jd=JD, profile=profile)
    log_outcome(user, "TestCo", "AI Engineer", "applied")
    result = _run(agent_env.pg)
    assert result["email"] == {"skipped": "Gmail is not configured (GMAIL_CREDENTIALS_FILE)"}
    assert store.get_application(app_id)["follow_up_due"] is True


def test_the_follow_up_text_passes_the_letter_validator(profile):
    from backend.resume_engine import cover_letter
    app = {"id": 1, "title": "Senior Staff Software Engineer- Search Quality",
           "company": "databricks"}
    assert cover_letter.letter_review(followups.paragraphs_for(app), profile,
                                      {"title": app["title"], "company": app["company"]}) == []


def test_a_dead_follow_up_fails_its_pending_draft(agent_env, profile):
    pg = agent_env.pg
    _, app_id = _application(pg, profile=profile)
    followups.begin_follow_up_draft(app_id, followups.message_id_for(app_id))
    followups.on_follow_up_dead({"application_id": app_id}, "RuntimeError: boom\n\ntb")
    [action] = [a for a in _actions(app_id) if a["action"] == "gmail_follow_up"]
    assert action["status"] == "failed"
    assert store.get_application(app_id)["error"].startswith("Follow-up failed: RuntimeError")


def _actions(app_id):
    from backend.agent import decisions
    return decisions.list_actions(app_id)
