"""Exactly one Gmail draft, even when the decision worker is killed between
Gmail creating the draft and the action row being marked done: the retry
finds the draft by its deterministic Message-ID instead of creating a
second one. Real subprocesses, a real kill (SIGKILL on POSIX,
TerminateProcess on Windows), real Postgres, Gmail mocked (a file-backed
fake HTTP layer; a /send URL fails). Run with -s to see the trace."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.agent import application_graph, checkpoint, decisions, run_graph, store  # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.llm.fake_provider import read_call_log  # noqa: E402
from backend.worker import Worker                 # noqa: E402
from tests._fake_gmail import FakeGmailSession    # noqa: E402

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "_decision_worker_harness.py"
LEASE_S = 2.0
JD = ("Build RAG pipelines with FAISS and LLM tooling for clinical Q&A. "
      "Send your resume to careers@testco.example.")


def _spawn(env, log_path, args):
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(HARNESS), *args], cwd=ROOT, env=env,
                            stdout=log, stderr=subprocess.STDOUT)
    proc._log = log
    return proc


def _finish(proc, timeout):
    try:
        return proc.wait(timeout=timeout)
    finally:
        proc._log.close()


def _gmail_action(pg, app_id):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT status, external_id, message_id FROM application_actions "
            "WHERE application_id = :a AND action = 'gmail_draft'"),
            {"a": app_id}).mappings().first()


def test_a_decision_worker_killed_after_gmail_create_leaves_exactly_one_draft(
        agent_env, monkeypatch, tmp_path):
    pg = agent_env.pg
    corpus.upsert_jobs([{"title": "AI Engineer", "company": "TestCo",
                         "location": "Bengaluru, India", "tags": [], "seniority": "Junior",
                         "clean_description": JD, "apply_link": "https://x/1",
                         "source": "ats"}])
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    run_graph.request_agent_run(trigger="api")
    assert Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                  lease_s=30).run_once().outcome == "succeeded"
    assert Worker({"application": application_graph.run_application_task},
                  lease_s=30).run_once().outcome == "succeeded"
    [app] = store.list_applications()
    app_id = app["id"]
    made = decisions.submit_decision(app_id, action="approve", version=1)
    task_id = made["task_id"]

    mailbox_path, marker = tmp_path / "mailbox.json", tmp_path / "created.marker"
    mailbox = FakeGmailSession(mailbox_path)
    llm_log = tmp_path / "llm.jsonl"
    with pg.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar()
    env = {k: v for k, v in os.environ.items()
           if k not in ("FAKE_LLM_SLEEP", "FAKE_LLM_RAISE")}
    env.update(PGOPTIONS=f"-csearch_path={schema}", PYTHONUNBUFFERED="1",
               LLM_PROVIDER="fake", FAKE_LLM_LOG=str(llm_log),
               GMAIL_CREDENTIALS_FILE=str(tmp_path / "client.json"))
    args = ["--apps-dir", str(agent_env.apps), "--mailbox", str(mailbox_path),
            "--lease", str(LEASE_S)]

    # Worker A: Gmail creates the draft, then A is killed before the row is done.
    proc_a = _spawn(env, tmp_path / "worker_a.log",
                    args + ["--hold-after-create", str(marker)])
    deadline = time.monotonic() + 90
    while not marker.exists():
        assert proc_a.poll() is None, (tmp_path / "worker_a.log").read_text()
        assert time.monotonic() < deadline, "worker A never created the draft"
        time.sleep(0.05)
    time.sleep(LEASE_S)                    # alive and heartbeating meanwhile
    with pg.connect() as conn:
        worker_a = conn.execute(text("SELECT locked_by FROM tasks WHERE id = :i"),
                                {"i": task_id}).scalar()
    proc_a.kill()
    code_a = _finish(proc_a, 30)
    if os.name == "posix":
        assert code_a == -signal.SIGKILL
    killed_task = taskqueue.get_task(task_id)
    pending = _gmail_action(pg, app_id)
    with checkpoint.open_checkpointer() as saver:
        graph = application_graph.build_application_graph().compile(checkpointer=saver)
        snap = graph.get_state(checkpoint.thread_config(store.application_thread(app_id)))
    drafts_after_a = mailbox.drafts()
    print(f"\n[gmail-kill] worker A {worker_a} killed (exit {code_a}) after Gmail "
          f"create returned ({marker.read_text().strip()}): task status="
          f"{killed_task['status']} attempts={killed_task['attempts']}")
    print(f"[gmail-kill] gmail_draft action row: {dict(pending)}; checkpoint next="
          f"{snap.next}; drafts in the mailbox: {[d['id'] for d in drafts_after_a]}")
    assert len(drafts_after_a) == 1
    assert pending["status"] == "pending" and pending["external_id"] is None
    assert pending["message_id"] == f"<aja-{app_id}-1@ai-job-agent>"
    assert snap.next == ("email_draft",)
    assert store.get_application(app_id)["status"] == "ready_to_apply"

    # Worker B reclaims the task once A's lease lapses: it finds the draft.
    requests_before_b = len(mailbox.requests())
    proc_b = _spawn(env, tmp_path / "worker_b.log", args)
    code_b = _finish(proc_b, 120)
    out_b = (tmp_path / "worker_b.log").read_text(encoding="utf-8")
    assert code_b == 0, out_b
    done = taskqueue.get_task(task_id)
    action = _gmail_action(pg, app_id)
    drafts = mailbox.drafts()
    b_requests = [(m, u.rsplit("/v1/", 1)[1], p) for m, u, p in
                  mailbox.requests()[requests_before_b:]]
    app = store.get_application(app_id)
    with pg.connect() as conn:
        approved = conn.execute(text(
            "SELECT count(*) FROM events WHERE event_type = 'approved'")).scalar()
    print(f"[gmail-kill] worker B {done['locked_by']} (exit {code_b}): task status="
          f"{done['status']} attempts={done['attempts']} outcome="
          f"{done['result']['outcome']} last_error={done['last_error']!r}")
    print(f"[gmail-kill] worker B's Gmail calls: {b_requests}")
    print(f"[gmail-kill] drafts in the mailbox: {[d['id'] for d in drafts]} "
          f"(Message-IDs {[d['message_id'] for d in drafts]}); action row: {dict(action)}")
    print(f"[gmail-kill] application {app_id}: status={app['status']}, approved events="
          f"{approved}, LLM calls in both workers={dict(read_call_log(llm_log))}, "
          f"send attempts={mailbox.send_attempts()}")

    assert done["status"] == "succeeded" and done["attempts"] == 2
    assert done["locked_by"] != worker_a
    assert f"lease expired (worker {worker_a}, attempt 1/3)" in done["last_error"]
    assert len(drafts) == 1                                   # EXACTLY one draft
    assert action["status"] == "done" and action["external_id"] == drafts[0]["id"]
    assert all(m == "GET" for m, _, _ in b_requests)          # B created nothing
    assert any(p.get("q") == f"rfc822msgid:aja-{app_id}-1@ai-job-agent"
               for _, _, p in b_requests)
    assert app["status"] == "email_drafted"
    assert approved == 1
    assert sum(read_call_log(llm_log).values()) == 0          # resume: no LLM at all
    assert mailbox.send_attempts() == []
    assert taskqueue.complete(task_id, worker_a, {"by": "a"}) is False
