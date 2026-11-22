"""
ranking.py -- ranking as a queued task (ADR-001 §2).

The one ranking service shared by the API (POST /v1/rankings), the Streamlit
UI and the worker, so data/ranked_jobs.json has a single writer: the worker
running a "rank" task (audit M7). The only in-process ranking left is the
DB-less Streamlit demo (ui/views/jobs.py), where there is no queue at all.

request_ranking() enqueues (API + UI); status_from_task() maps the latest
rank task onto the v1 RankingStatus contract; run_rank_task() is the worker
handler for kind "rank":
  load profile -> strict DB corpus -> rank_jobs -> atomic write of RANKED_FILE
It is a pure function of (profile, corpus), and the write is an atomic
replace, so re-running it (a retry, or a second worker after a lease
expired) is safe.
"""
from __future__ import annotations

import json
from typing import Any

from ..config import PROFILE_FILE, RANKED_FILE
from ..database.corpus import load_corpus_snapshot, write_json_atomic
from ..version import FORMULA_VERSION

RANK_KIND = "rank"
# One ranking run at a time: every rank task holds this key while queued or
# running (partial unique index on tasks), which is what makes a second
# POST /v1/rankings a 409.
RANK_CONCURRENCY_KEY = "rank:default"
RANK_MAX_ATTEMPTS = 3


def _rank_jobs(profile: dict, jobs: list[dict]) -> list[dict]:
    # Heavy import (sentence-transformers/torch) deferred to the first rank
    # task, so the worker boots fast; tests replace this seam.
    from ..matching_engine.matcher import rank_jobs
    return rank_jobs(profile, jobs, candidate_is_fresher=True)


def _load_profile() -> dict:
    if not PROFILE_FILE.exists():
        raise FileNotFoundError(f"no candidate profile at {PROFILE_FILE}; "
                                "upload a resume first (POST /v1/resumes)")
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def corpus_version_str(version: tuple) -> str:
    """CorpusSnapshot.version as a JSON-safe string, e.g.
    "db:3875:2026-11-22T10:00:00+00:00"."""
    return ":".join(v.isoformat() if hasattr(v, "isoformat") else str(v)
                    for v in version)


def queue_configured() -> bool:
    """Ranking runs through the queue exactly when a database is configured
    (DATABASE_URL or DB_HOST). Unconfigured = the DB-less demo."""
    from ..database.corpus import db_configured
    return db_configured()


def request_ranking(*, conn: Any = None) -> int:
    """Queue a ranking run; returns the task id. The API and the UI both
    come through here.

    Raises taskqueue.ConcurrencyConflict while a rank task is queued or
    running, and CorpusUnavailable when the worker would have nothing to
    rank (no reachable DB corpus) -- checked up front so the caller gets
    the error now instead of a task that dies after its retries."""
    from .. import taskqueue
    load_corpus_snapshot(require_db=True)
    return taskqueue.enqueue(RANK_KIND, {}, concurrency_key=RANK_CONCURRENCY_KEY,
                             max_attempts=RANK_MAX_ATTEMPTS, conn=conn)


def latest_rank_task() -> dict | None:
    from .. import taskqueue
    return taskqueue.latest_task(kind=RANK_KIND,
                                 concurrency_key=RANK_CONCURRENCY_KEY)


def _error_summary(last_error: str | None) -> str:
    """The 'Type: message' paragraph of a worker-recorded error (the
    traceback after the blank line stays in the tasks row)."""
    return (last_error or "").split("\n\n", 1)[0].strip()


def status_from_task(task: dict | None, ranked_file_exists: bool) -> dict:
    """Map the latest rank task onto the RankingStatus contract
    (state: idle | running | done | failed), unchanged from v1:

      no task       -> done "loaded from previous run" if RANKED_FILE
                       exists, else idle
      queued        -> running (waiting for a worker)
      running       -> running
      queued again  -> running, "retrying" (an attempt failed)
      succeeded     -> done, ranked_count from the task result
      dead          -> failed, detail = the last error
    """
    if task is None:
        if ranked_file_exists:
            return {"state": "done", "detail": "loaded from previous run",
                    "ranked_count": None}
        return {"state": "idle", "detail": "", "ranked_count": None}

    tid, n, m = task["id"], task["attempts"], task["max_attempts"]
    status = task["status"]
    if status == "queued" and n == 0:
        detail = f"Task #{tid} queued, waiting for a worker (attempt 0/{m})."
    elif status == "queued":
        detail = (f"Task #{tid} retrying: attempt {n}/{m} failed "
                  f"({_error_summary(task.get('last_error'))}).")
    elif status == "running":
        detail = f"Task #{tid} running (attempt {n}/{m})."
    elif status == "succeeded":
        return {"state": "done", "detail": "",
                "ranked_count": (task.get("result") or {}).get("ranked_count")}
    else:                                   # dead (or the unused 'failed')
        return {"state": "failed",
                "detail": _error_summary(task.get("last_error"))
                or f"Task #{tid} failed after {n}/{m} attempts.",
                "ranked_count": None}
    return {"state": "running", "detail": detail, "ranked_count": None}


def ranking_status(ranked_file_exists: bool) -> dict:
    """RankingStatus for the latest rank task (queue mode only)."""
    return status_from_task(latest_rank_task(), ranked_file_exists)


def run_rank_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "rank" tasks. Ranks against the Postgres corpus
    only (strict mode: CorpusUnavailable instead of a stale JSON
    fallback). If the worker lost its lease while ranking (ctx.lease_lost),
    the output is NOT written: the task now belongs to another worker."""
    profile = _load_profile()
    snap = load_corpus_snapshot(require_db=True)
    ranked = _rank_jobs(profile, snap.jobs)
    if ctx is not None:
        ctx.ensure_lease()
    write_json_atomic(RANKED_FILE, ranked)
    return {"ranked_count": len(ranked),
            "corpus_version": corpus_version_str(snap.version),
            "formula_version": FORMULA_VERSION}
