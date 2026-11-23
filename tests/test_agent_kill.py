"""An application worker killed mid-graph (in tailor_resume) is resumed by
another worker from its last LangGraph checkpoint: the nodes it finished
never run again. Real subprocesses, a real kill (SIGKILL on POSIX,
TerminateProcess on Windows), real Postgres. Run with -s to see the trace."""
import hashlib
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
from backend.agent import application_graph, checkpoint, run_graph, store  # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.llm.fake_provider import read_call_log  # noqa: E402
from backend.worker import Worker                 # noqa: E402

pytestmark = pytest.mark.db

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "tests" / "_agent_worker_harness.py"
LEASE_S = 2.0


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


def _task_row(pg, task_id):
    with pg.connect() as conn:
        return conn.execute(text("SELECT * FROM tasks WHERE id = :i"),
                            {"i": task_id}).mappings().one()


def test_a_killed_application_worker_resumes_from_its_last_checkpoint(
        agent_env, monkeypatch, tmp_path):
    pg = agent_env.pg
    corpus.upsert_jobs([{"title": "AI Engineer", "company": "TestCo",
                         "location": "Remote", "tags": [], "seniority": "Junior",
                         "clean_description": "Build RAG pipelines with FAISS and LLMs.",
                         "apply_link": "https://x/1", "source": "ats"}])
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    run_graph.request_agent_run(trigger="api")
    assert Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                  lease_s=30).run_once().outcome == "succeeded"
    [app] = store.list_applications()
    app_id = app["id"]
    task_id = store.application_task(app_id)["id"]

    llm_log, render_log = tmp_path / "llm.jsonl", tmp_path / "render.log"
    with pg.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar()
    env = {k: v for k, v in os.environ.items()
           if k not in ("FAKE_LLM_SLEEP", "FAKE_LLM_RAISE")}
    env.update(PGOPTIONS=f"-csearch_path={schema}", PYTHONUNBUFFERED="1",
               LLM_PROVIDER="fake", FAKE_LLM_LOG=str(llm_log))
    args = ["--profile-file", str(agent_env.profile_file), "--apps-dir",
            str(agent_env.apps), "--render-log", str(render_log), "--lease", str(LEASE_S)]

    # Worker A: its fake LLM sleeps in tailor_resume; it is killed there.
    proc_a = _spawn({**env, "FAKE_LLM_SLEEP": "tailor_resume=120"},
                    tmp_path / "worker_a.log", args)
    deadline = time.monotonic() + 90
    while read_call_log(llm_log)["tailor_resume"] < 1:
        assert proc_a.poll() is None, (tmp_path / "worker_a.log").read_text()
        assert time.monotonic() < deadline, "worker A never reached tailor_resume"
        time.sleep(0.05)
    time.sleep(LEASE_S)                    # alive and heartbeating meanwhile
    running = _task_row(pg, task_id)
    worker_a = running["locked_by"]
    proc_a.kill()
    code_a = _finish(proc_a, 30)
    if os.name == "posix":
        assert code_a == -signal.SIGKILL
    killed = _task_row(pg, task_id)
    with checkpoint.open_checkpointer() as saver:
        graph = application_graph.build_application_graph().compile(checkpointer=saver)
        snap = graph.get_state(checkpoint.thread_config(store.application_thread(app_id)))
    print(f"\n[kill] worker A {worker_a} killed (exit {code_a}) in tailor_resume: "
          f"task status={killed['status']} attempts={killed['attempts']}; "
          f"checkpoint next={snap.next}; llm calls so far={dict(read_call_log(llm_log))}")
    assert (killed["status"], killed["attempts"], killed["locked_by"]) == \
        ("running", 1, worker_a)
    assert snap.next == ("tailor_resume",)
    assert set(snap.values) >= {"job", "profile", "jd_requirements"}
    assert "tailored" not in snap.values
    assert not render_log.exists()
    assert store.get_application(app_id)["status"] == "drafting"

    # Worker B (no sleep) reclaims the task once A's lease lapses.
    proc_b = _spawn(env, tmp_path / "worker_b.log", args)
    code_b = _finish(proc_b, 120)
    out_b = (tmp_path / "worker_b.log").read_text(encoding="utf-8")
    assert code_b == 0, out_b
    done = _task_row(pg, task_id)
    calls = read_call_log(llm_log)
    renders = render_log.read_text(encoding="utf-8").split("\n")
    renders = [line.split() for line in renders if line]
    app = store.get_application(app_id)
    artifacts = store.get_artifacts(app_id)
    with pg.connect() as conn:
        drafted = conn.execute(text(
            "SELECT count(*) FROM events WHERE event_type = 'drafted'")).scalar()
    print(f"[kill] worker B {done['locked_by']} (exit {code_b}): task status="
          f"{done['status']} attempts={done['attempts']} "
          f"last_error={done['last_error']!r}")
    print(f"[kill] llm calls per step across both workers: {dict(calls)}")
    # The interpreter's pid, from the worker id (host:pid:uuid). Popen.pid
    # can be a launcher's (a Windows venv python.exe re-executes itself).
    pid_b = int(done["locked_by"].split(":")[-2])
    print(f"[kill] renders: {[r[0] for r in renders]} (pids {sorted({r[1] for r in renders})}, "
          f"worker B pid {pid_b})")
    print(f"[kill] application {app_id}: status={app['status']}, artifacts="
          f"{sorted(a['kind'] for a in artifacts)}, drafted events={drafted}")

    assert done["status"] == "succeeded" and done["attempts"] == 2
    assert done["locked_by"] != worker_a
    assert f"lease expired (worker {worker_a}, attempt 1/3)" in done["last_error"]
    assert calls["extract_requirements"] == 1           # never re-run
    assert calls["tailor_resume"] == 2                  # killed once, then finished
    assert calls["cover_letter"] == 1
    assert sorted(r[0] for r in renders) == ["cover_letter.docx", "cover_letter.txt",
                                             "resume_docx"]   # written once
    assert {int(r[1]) for r in renders} == {pid_b}            # all by worker B
    assert app["status"] == "drafted"
    assert len(artifacts) == 4
    for a in artifacts:
        assert a["sha256"] == hashlib.sha256(Path(a["path"]).read_bytes()).hexdigest()
    assert drafted == 1
    # The dead worker can no longer touch the task.
    assert taskqueue.complete(task_id, worker_a, {"by": "a"}) is False
