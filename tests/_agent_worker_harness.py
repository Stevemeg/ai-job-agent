"""Subprocess worker for tests/test_agent_kill.py (not collected by pytest).

A real backend.worker.Worker running the real "application" handler (the
LangGraph application graph and its Postgres checkpointer), pointed at the
test's files, and logging every document render to --render-log (one line
per render: "<kind> <pid>", fsynced) so the test can count them across
processes. The LLM is the fake provider, configured by the environment
(LLM_PROVIDER=fake, FAKE_LLM_SLEEP, FAKE_LLM_LOG); the database by DB_* /
DATABASE_URL plus PGOPTIONS=-csearch_path=<the test's schema>.

    python tests/_agent_worker_harness.py --profile-file P --apps-dir D \\
        --render-log L [--lease 2] [--max-wait 90]

Exits after one task: 0 if it succeeded.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.agent import application_graph        # noqa: E402
from backend.resume_engine import cover_letter, resume_tailor  # noqa: E402
from backend.services import ranking               # noqa: E402
from backend.worker import Worker                  # noqa: E402


def _logging(render_log: Path, fn, kind_of):
    def wrapper(*args, **kwargs):
        out = fn(*args, **kwargs)
        with open(render_log, "a", encoding="utf-8") as f:
            f.write(f"{kind_of(args)} {os.getpid()}\n")
            f.flush()
            os.fsync(f.fileno())
        return out
    return wrapper


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-file", required=True)
    parser.add_argument("--apps-dir", required=True)
    parser.add_argument("--render-log", required=True)
    parser.add_argument("--lease", type=float, default=2.0)
    parser.add_argument("--max-wait", type=float, default=90.0)
    args = parser.parse_args()

    ranking.PROFILE_FILE = Path(args.profile_file)
    application_graph.APPLICATIONS_DIR = Path(args.apps_dir)
    log = Path(args.render_log)
    resume_tailor.render_resume_docx = _logging(
        log, resume_tailor.render_resume_docx, lambda a: "resume_docx")
    cover_letter.render_cover_letter = _logging(
        log, cover_letter.render_cover_letter,
        lambda a: "cover_letter" + Path(a[1]).suffix)

    worker = Worker({"application": application_graph.run_application_task},
                    lease_s=args.lease, poll_min_s=0.1, poll_max_s=0.5)
    print(f"worker_id={worker.worker_id}", flush=True)
    deadline = time.monotonic() + args.max_wait
    while time.monotonic() < deadline:
        result = worker.run_once()
        if result is not None:
            print("result=" + json.dumps(result.__dict__), flush=True)
            return 0 if result.outcome == "succeeded" else 1
        time.sleep(0.2)
    print("timed out waiting for a claimable task", flush=True)
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
