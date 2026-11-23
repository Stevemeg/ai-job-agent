"""The application graph against a real Postgres, in process: the happy
path, validator flags, an LLM outage that kills the task, and retries that
resume from the last checkpoint. (A real SIGKILL is in test_agent_kill.py.)"""
import hashlib
import json

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.agent import application_graph, run_graph, store  # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.worker import Worker                 # noqa: E402

pytestmark = pytest.mark.db

JD = ("Build RAG pipelines with FAISS and LLM tooling for clinical Q&A. "
      "Python, Pandas and Git required.")
LLM_NODES = ("extract_requirements", "tailor_resume", "cover_letter")


def _dispatch_one(env, monkeypatch, n_jobs=1):
    corpus.upsert_jobs([{"title": f"AI Engineer {'I' * (i + 1)}", "company": "TestCo",
                         "location": "Bengaluru, India", "tags": [], "seniority": "Junior",
                         "experience": "1 years", "clean_description": JD,
                         "apply_link": f"https://x/{i}", "source": "ats"}
                        for i in range(n_jobs)])
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    run_graph.request_agent_run(trigger="api")
    run = Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                 lease_s=30).run_once()
    assert run.outcome == "succeeded", run.error
    with env.pg.connect() as conn:
        return conn.execute(text("SELECT id FROM applications ORDER BY id")).scalars().all()


def _app_worker():
    return Worker({"application": application_graph.run_application_task}, lease_s=30)


def _make_due(pg):
    with pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now() WHERE status = 'queued'"))


def _drafted_events(pg):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT user_id, job_id, match_score, score_breakdown, note FROM events "
            "WHERE event_type = 'drafted'")).all()


def _llm_calls(env):
    return {k: env.llm.calls[k] for k in LLM_NODES}


def _expiry_tasks(pg):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT payload, idempotency_key, status, "
            "round(extract(epoch FROM run_after - now()) / 86400) AS days "
            "FROM tasks WHERE kind = 'approval_expiry' ORDER BY id")).all()


def test_happy_path_drafts_four_artifacts_and_logs_one_event(agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)
    agent_env.llm.calls.clear()                      # count the application only
    run = _app_worker().run_once()
    assert run.outcome == "succeeded", run.error

    app = store.get_application(app_id)
    assert (app["status"], app["error"]) == ("awaiting_approval", None)
    assert (app["needs_review"], app["version"]) == (False, 1)
    # Its approval expiry, due in AGENT_APPROVAL_TTL_DAYS (7), keyed per version.
    [expiry] = _expiry_tasks(agent_env.pg)
    assert expiry.payload == {"application_id": app_id, "version": 1}
    assert (expiry.idempotency_key, expiry.status, expiry.days) == (
        f"expiry:{app_id}:1", "queued", 7)
    artifacts = {a["kind"]: a for a in store.get_artifacts(app_id)}
    assert set(artifacts) == {"resume_docx", "cover_letter_docx", "cover_letter_txt",
                              "jd_requirements"}
    folder = agent_env.apps / str(app_id)
    for kind, a in artifacts.items():
        path = folder / application_graph.ARTIFACT_FILES[kind]
        assert a["path"] == str(path)
        assert a["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifacts["resume_docx"]["report"]["ok"] is True
    assert artifacts["cover_letter_docx"]["report"] == {"ok": True, "paragraphs": []}
    assert artifacts["jd_requirements"]["report"] is None
    assert "RAG" in json.loads((folder / "jd_requirements.json").read_text())[
        "must_have_skills"]
    assert (folder / "cover_letter.txt").read_text(encoding="utf-8").splitlines()[4] \
        == f"Re: {app['title']} -- TestCo"

    [event] = _drafted_events(agent_env.pg)
    assert (event.user_id, event.job_id) == (app["user_id"], app["job_id"])
    assert float(event.match_score) == app["match_score"]
    assert event.score_breakdown and "nothing was sent" in event.note
    with agent_env.pg.connect() as conn:
        assert conn.execute(text(
            "SELECT source FROM events WHERE event_type = 'drafted'")).scalar() == "agent"
    # One LLM call per LLM node; the requirements were extracted once and
    # shared by both documents.
    assert _llm_calls(agent_env) == {"extract_requirements": 1, "tailor_resume": 1,
                                     "cover_letter": 1}
    task = taskqueue.get_task(run.task_id)
    assert task["result"]["status"] == "awaiting_approval"
    assert (task["result"]["needs_review"], task["result"]["version"]) == (False, 1)
    assert task["result"]["artifacts"] == {k: a["sha256"] for k, a in artifacts.items()}

    # Running it again (a retry after the graph finished): nothing re-runs,
    # nothing is logged twice.
    again = application_graph.run_application_task({"application_id": app_id})
    assert again["status"] == "awaiting_approval"
    assert _llm_calls(agent_env) == {"extract_requirements": 1, "tailor_resume": 1,
                                     "cover_letter": 1}
    assert store.persist(app_id, {}, needs_review=False, version=1) is False
    assert len(_drafted_events(agent_env.pg)) == 1           # idempotent
    assert len(_expiry_tasks(agent_env.pg)) == 1


def test_a_resume_flag_means_needs_review(agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)

    def fabricate(prompt, answer):
        answer["projects"][0]["tailored_bullets"].append(
            "Deployed it on Databricks for 40,000 clinicians.")
        return answer
    agent_env.llm.on("tailor_resume", fabricate)
    assert _app_worker().run_once().outcome == "succeeded"
    app = store.get_application(app_id)
    assert (app["status"], app["needs_review"]) == ("awaiting_approval", True)
    report = {a["kind"]: a["report"] for a in store.get_artifacts(app_id)}["resume_docx"]
    assert report["ok"] is False
    flagged = next(p for p in report["projects"] if not p["ok"])
    assert {"Databricks", "40000"} <= set(flagged["flagged"])
    assert len(_drafted_events(agent_env.pg)) == 1


def test_a_cover_letter_flag_means_needs_review(agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)
    agent_env.llm.on("cover_letter", lambda prompt, answer: {
        "paragraphs": answer["paragraphs"][:2] + ["I led a Kubernetes team of 12."]})
    assert _app_worker().run_once().outcome == "succeeded"
    app = store.get_application(app_id)
    assert (app["status"], app["needs_review"]) == ("awaiting_approval", True)
    letter = {a["kind"]: a["report"] for a in store.get_artifacts(app_id)}[
        "cover_letter_txt"]
    assert letter["ok"] is False and letter["paragraphs"][0]["paragraph"] == 3


def test_an_llm_outage_retries_with_backoff_then_fails_the_application(
        agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)
    monkeypatch.setenv("FAKE_LLM_RAISE", "extract_requirements=unavailable")
    agent_env.llm.calls.clear()
    worker = _app_worker()

    first = worker.run_once()
    task = taskqueue.get_task(first.task_id)
    assert first.outcome == "failed" and task["status"] == "queued"
    with agent_env.pg.connect() as conn:
        assert conn.execute(text(
            "SELECT run_after > now() FROM tasks WHERE id = :i"),
            {"i": first.task_id}).scalar()                       # backed off
    assert store.get_application(app_id)["status"] == "drafting"

    for _ in range(2):
        _make_due(agent_env.pg)
        assert worker.run_once().outcome == "failed"
    task = taskqueue.get_task(first.task_id)
    assert (task["status"], task["attempts"]) == ("dead", 3)
    assert task["last_error"].startswith("LLMUnavailableError: fake LLM provider")

    app = store.get_application(app_id)
    assert app["status"] == "failed"
    assert app["error"] == ("LLMUnavailableError: fake LLM provider: "
                            "extract_requirements unavailable (FAKE_LLM_RAISE)")
    assert store.get_artifacts(app_id) == []
    assert not (agent_env.apps / str(app_id)).exists()
    assert _drafted_events(agent_env.pg) == []
    assert _expiry_tasks(agent_env.pg) == []
    assert _llm_calls(agent_env) == {"extract_requirements": 3, "tailor_resume": 0,
                                     "cover_letter": 0}


def test_a_retry_resumes_at_the_node_that_failed(agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)
    agent_env.llm.calls.clear()
    failures = {"left": 1}

    def flaky(prompt, answer):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("provider hiccup")
        return answer
    agent_env.llm.on("cover_letter", flaky)
    worker = _app_worker()
    assert worker.run_once().outcome == "failed"
    _make_due(agent_env.pg)
    run = worker.run_once()
    assert run.outcome == "succeeded"
    assert taskqueue.get_task(run.task_id)["attempts"] == 2
    assert store.get_application(app_id)["status"] == "awaiting_approval"
    # extract + tailor ran once; only the cover letter node ran again.
    assert _llm_calls(agent_env) == {"extract_requirements": 1, "tailor_resume": 1,
                                     "cover_letter": 2}


def test_a_task_whose_last_lease_expired_fails_its_application(agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)
    with agent_env.pg.begin() as conn:                 # its third worker died
        conn.execute(text(
            "UPDATE tasks SET status = 'running', attempts = max_attempts, "
            "locked_by = 'gone', lease_expires_at = now() - interval '1 second' "
            "WHERE kind = 'application'"))
    assert _app_worker().run_once() is None            # claim marks it dead
    app = store.get_application(app_id)
    assert app["status"] == "failed"
    assert app["error"].startswith("lease expired (worker gone, attempt 3/3)")


def test_a_dead_task_never_overwrites_a_drafted_application(agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)
    assert _app_worker().run_once().outcome == "succeeded"
    application_graph.on_application_dead({"application_id": app_id}, "Boom: late")
    assert store.get_application(app_id)["status"] == "awaiting_approval"


def test_persist_never_redrafts_a_decided_application(agent_env, monkeypatch):
    [app_id] = _dispatch_one(agent_env, monkeypatch)
    assert _app_worker().run_once().outcome == "succeeded"
    with agent_env.pg.begin() as conn:
        conn.execute(text("UPDATE applications SET status = 'declined'"))
    with pytest.raises(store.NotDraftable):
        store.persist(app_id, {}, needs_review=False, version=2)
    assert store.get_application(app_id)["version"] == 1
