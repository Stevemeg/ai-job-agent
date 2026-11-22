"""Subprocess worker for tests/test_worker_kill.py (not collected by pytest).

Runs a real backend.worker.Worker whose only handler, "slow", is slow on
its first attempt and fast on retries, and leaves markers in the task's
payload["marker_dir"]:
    started-<attempt>.json   {"worker_id", "pid"} when an attempt starts
    completions.log          one line (worker id) per handler that finished

The database comes from the usual DB_* / DATABASE_URL environment; the test
passes PGOPTIONS=-csearch_path=<its throwaway schema>.

    python tests/_worker_harness.py --lease 2 --once      # exit after 1 task
    python tests/_worker_harness.py --lease 2 --forever   # until SIGTERM
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.worker import Worker            # noqa: E402


def slow(payload, ctx):
    marker_dir = Path(payload["marker_dir"])
    attempt = ctx.task.attempts
    (marker_dir / f"started-{attempt}.json").write_text(
        json.dumps({"worker_id": ctx.worker_id, "pid": os.getpid()}),
        encoding="utf-8")
    seconds = payload["first_attempt_s"] if attempt == 1 else payload["retry_s"]
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.05)
    with open(marker_dir / "completions.log", "a", encoding="utf-8") as f:
        f.write(ctx.worker_id + "\n")
    return {"by": ctx.worker_id, "attempt": attempt}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lease", type=float, default=2.0)
    parser.add_argument("--grace", type=float, default=10.0)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--forever", action="store_true")
    parser.add_argument("--max-wait", type=float, default=60.0)
    args = parser.parse_args()

    worker = Worker({"slow": slow}, lease_s=args.lease, grace_s=args.grace,
                    poll_min_s=0.1, poll_max_s=0.5)
    print(f"worker_id={worker.worker_id}", flush=True)
    if args.forever:
        return worker.run_forever()

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
