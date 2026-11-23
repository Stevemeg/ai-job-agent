"""Exactly one follow-up Gmail draft, even when the follow_up worker is killed
between Gmail creating the draft and the action row being marked done: the
retry finds the draft by its deterministic Message-ID instead of creating
a second one. Real subprocesses, a real kill (SIGKILL on POSIX,
TerminateProcess on Windows), real Postgres, Gmail mocked (a file-backed
fake HTTP layer; a /send URL fails). Run with -s to see the trace."""
import json
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
from backend.agent import followups, store        # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.database.events import log_outcome   # noqa: E402
from _fake_gmail import FakeGmailSession          # noqa: E402

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "_decision_worker_harness.py"
LEASE_S = 2.0
JD = "Build RAG pipelines with FAISS. Send questions to careers@testco.example."


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


def _action(pg, app_id):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT status, external_id, message_id FROM application_actions "
            "WHERE application_id = :a AND action = 'gmail_follow_up'"),
            {"a": app_id}).mappings().first()


def test_a_follow_up_worker_killed_after_gmail_create_leaves_exactly_one_draft(
        agent_env, profile, tmp_path):
    pg = agent_env.pg
    corpus.upsert_jobs([{"title": "AI Engineer", "company": "TestCo", "location": "Pune",
                         "tags": [], "clean_description": JD, "apply_link": "https://x/1",
                         "source": "ats"}])
    user = store.user_for_profile(profile)
    with pg.connect() as conn:
        job_id = conn.execute(text("SELECT id FROM canonical_jobs")).scalar()
    app_id = store.dispatch(user_id=user, job_id=job_id, match_score=30, profile_hash="p",
                            cache_key="k")["application_id"]
    with pg.begin() as conn:
        conn.execute(text("UPDATE applications SET status = 'ready_to_apply', version = 1 "
                          "WHERE id = :a"), {"a": app_id})
    log_outcome(user, "TestCo", "AI Engineer", "applied")
    with pg.begin() as conn:
        task_id = conn.execute(text(
            "UPDATE tasks SET run_after = now() WHERE kind = 'follow_up' RETURNING id")).scalar()

    mailbox_path, marker = tmp_path / "mailbox.json", tmp_path / "created.marker"
    mailbox = FakeGmailSession(mailbox_path)
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    with pg.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar()
    env = {k: v for k, v in os.environ.items()
           if k not in ("FAKE_LLM_SLEEP", "FAKE_LLM_RAISE")}
    env.update(PGOPTIONS=f"-csearch_path={schema}", PYTHONUNBUFFERED="1",
               LLM_PROVIDER="fake", GMAIL_CREDENTIALS_FILE=str(tmp_path / "client.json"))
    args = ["--kind", "follow_up", "--apps-dir", str(agent_env.apps), "--mailbox",
            str(mailbox_path), "--profile-file", str(profile_file), "--lease", str(LEASE_S)]

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
    pending = _action(pg, app_id)
    drafts_after_a = mailbox.drafts()
    print(f"\n[follow-up-kill] worker A {worker_a} killed (exit {code_a}) after Gmail "
          f"create returned ({marker.read_text().strip()}): task status="
          f"{killed_task['status']} attempts={killed_task['attempts']}")
    print(f"[follow-up-kill] gmail_follow_up action row: {dict(pending)}; "
          f"follow_up_due={store.get_application(app_id)['follow_up_due']}; "
          f"drafts in the mailbox: {[d['id'] for d in drafts_after_a]}")
    assert len(drafts_after_a) == 1
    assert pending["status"] == "pending" and pending["external_id"] is None
    assert pending["message_id"] == f"<aja-followup-{app_id}@ai-job-agent>"
    assert store.get_application(app_id)["follow_up_due"] is True

    # Worker B reclaims the task once A's lease lapses: it finds the draft.
    requests_before_b = len(mailbox.requests())
    proc_b = _spawn(env, tmp_path / "worker_b.log", args)
    code_b = _finish(proc_b, 120)
    out_b = (tmp_path / "worker_b.log").read_text(encoding="utf-8")
    assert code_b == 0, out_b
    done = taskqueue.get_task(task_id)
    action = _action(pg, app_id)
    drafts = mailbox.drafts()
    b_requests = [(m, u.rsplit("/v1/", 1)[1], p) for m, u, p in
                  mailbox.requests()[requests_before_b:]]
    print(f"[follow-up-kill] worker B {done['locked_by']} (exit {code_b}): task status="
          f"{done['status']} attempts={done['attempts']} result={done['result']} "
          f"last_error={done['last_error']!r}")
    print(f"[follow-up-kill] worker B's Gmail calls: {b_requests}")
    print(f"[follow-up-kill] drafts in the mailbox: {[d['id'] for d in drafts]} "
          f"(Message-IDs {[d['message_id'] for d in drafts]}); action row: {dict(action)}; "
          f"send attempts={mailbox.send_attempts()}")

    assert done["status"] == "succeeded" and done["attempts"] == 2
    assert done["locked_by"] != worker_a
    assert f"lease expired (worker {worker_a}, attempt 1/3)" in done["last_error"]
    assert len(drafts) == 1                                   # EXACTLY one draft
    assert action["status"] == "done" and action["external_id"] == drafts[0]["id"]
    assert done["result"]["email"]["draft_id"] == drafts[0]["id"]
    assert all(m == "GET" for m, _, _ in b_requests)          # B created nothing
    assert any(p.get("q") == f"rfc822msgid:aja-followup-{app_id}@ai-job-agent"
               for _, _, p in b_requests)
    assert mailbox.send_attempts() == []
    assert taskqueue.complete(task_id, worker_a, {"by": "a"}) is False
    assert followups.list_follow_ups_due(user)[0]["follow_up_draft"]["status"] == "done"
