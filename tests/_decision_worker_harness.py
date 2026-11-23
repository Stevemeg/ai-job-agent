"""Subprocess worker for tests/test_agent_gmail_kill.py (not collected by
pytest).

A real backend.worker.Worker running the real "application_decision"
handler (the LangGraph application graph resumed from its Postgres
checkpoint), with Gmail configured over the file-backed fake HTTP layer
(tests/_fake_gmail.py, --mailbox) so several processes share one mailbox.

--hold-after-create MARKER: once the Gmail create has RETURNED, and before
the gmail_draft action row is marked done, write MARKER (fsynced) and
sleep -- the window the test SIGKILLs this process in.

    python tests/_decision_worker_harness.py --apps-dir D --mailbox M \\
        [--hold-after-create MARKER] [--lease 2] [--max-wait 90]

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

from backend.agent import application_graph, decisions   # noqa: E402
from backend.integrations import gmail_auth              # noqa: E402
from backend.worker import Worker                        # noqa: E402
from tests._fake_gmail import FakeGmailSession            # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apps-dir", required=True)
    parser.add_argument("--mailbox", required=True)
    parser.add_argument("--hold-after-create", default=None)
    parser.add_argument("--lease", type=float, default=2.0)
    parser.add_argument("--max-wait", type=float, default=90.0)
    args = parser.parse_args()

    application_graph.APPLICATIONS_DIR = Path(args.apps_dir)
    mailbox = FakeGmailSession(args.mailbox)       # a send attempt raises (and is recorded)
    gmail_auth.authorized_session = lambda: mailbox

    if args.hold_after_create:
        real_done = decisions.mark_gmail_draft_done

        def hold(application_id, draft_id):
            with open(args.hold_after_create, "w", encoding="utf-8") as f:
                f.write(f"created {draft_id} {os.getpid()}\n")
                f.flush()
                os.fsync(f.fileno())
            time.sleep(300)                        # killed in here
            return real_done(application_id, draft_id)
        decisions.mark_gmail_draft_done = hold

    worker = Worker({decisions.DECISION_KIND: application_graph.run_decision_task},
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
