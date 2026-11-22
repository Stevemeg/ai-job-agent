"""
ranking.py -- ranking as a queued task (ADR-001 §2).

The one ranking service shared by the API (POST /v1/rankings), the Streamlit
UI, the CLIs and the worker. data/ranked_jobs.json is written in exactly one
place, _write_ranked() here (audit M7): by the worker running a "rank" task,
and by rank_and_publish() for the callers that rank in-process -- the
DB-less Streamlit demo (ui/views/jobs.py, which has no queue at all) and the
CLIs (backend.main, job_scraper.run_real_ranking).

request_ranking() enqueues (API + UI); status_from_task() maps the latest
rank task onto the v1 RankingStatus contract; run_rank_task() is the worker
handler for kind "rank", and execute_rank() the rank execution it shares
with the scan handler (services/scan.py):

  take the rank lock -> read the strict DB corpus -> score the delta
  (services/scoring.py) -> assemble -> atomic write of RANKED_FILE -> release

The rank lock is a Postgres SESSION-level advisory lock held on a dedicated
connection for the scoring and the write, so rank executions -- API- or
scan-triggered, on any number of workers, and the CLIs when a DB is
configured -- run one at a time. If the holder dies, its connection closes
and Postgres releases the lock; nothing has to expire. The corpus is read
only once the lock is held, so jobs saved while another rank was running
are never missed. Re-running is safe: scoring is a pure function of
(profile, job, formula) and the write is an atomic replace.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager, nullcontext
from typing import Any, Iterator

from ..config import PROFILE_FILE, RANKED_FILE
from ..database.corpus import (load_corpus_rows, load_corpus_snapshot,
                               require_database, write_json_atomic)
from . import scoring

log = logging.getLogger(__name__)

RANK_KIND = "rank"
# One ranking run at a time: every rank task holds this key while queued or
# running (partial unique index on tasks), which is what makes a second
# POST /v1/rankings a 409.
RANK_CONCURRENCY_KEY = "rank:default"
RANK_MAX_ATTEMPTS = 3

# pg_advisory_lock key of the rank lock (session-level, unlike the
# transaction-scoped migration/upsert locks in db.py and corpus.py).
RANK_LOCK_KEY = 0x72616E6B         # b"rank"
# How long a rank execution waits for the lock before failing the attempt
# (the task then retries with backoff). Long enough to queue behind a full
# re-rank of the corpus; finite so a hung holder can't block forever.
RANK_LOCK_WAIT_S = 1800.0
RANK_LOCK_POLL_S = 0.5


class RankLockTimeout(TimeoutError):
    """The rank lock stayed held by someone else for the whole wait."""


def _rank_jobs(profile: dict, jobs: list[dict]) -> list[dict]:
    # Full in-process ranking for rank_and_publish(). Heavy import
    # (sentence-transformers/torch) deferred; tests replace this seam.
    from ..matching_engine.matcher import rank_jobs
    return rank_jobs(profile, jobs,
                     candidate_is_fresher=scoring.CANDIDATE_IS_FRESHER)


def _make_scorer(profile: dict) -> scoring.Scorer:
    # The per-job scorer for incremental runs; tests replace this seam.
    return scoring.matcher_scorer(profile)


@contextmanager
def rank_lock(*, ctx: Any = None, wait_s: float | None = None) -> Iterator[None]:
    """Hold the rank lock for the duration of the block.

    A session-level pg_advisory_lock taken on a DEDICATED connection: it is
    detached from the pool (closing it ends the session -- and with it the
    lock -- instead of handing a locked connection to the next user) and in
    autocommit mode (no transaction is left open while it is held). A
    worker that dies mid-rank drops the connection, which releases the lock.

    Polls pg_try_advisory_lock every RANK_LOCK_POLL_S; while waiting it
    checks a worker `ctx`'s lease (LeaseLost if another worker took the
    task over) and gives up with RankLockTimeout after `wait_s`."""
    from sqlalchemy import text
    from ..database.db import get_engine

    wait_s = RANK_LOCK_WAIT_S if wait_s is None else wait_s
    conn = get_engine().connect()
    try:
        # Isolation level first: SQLAlchemy needs the pool record to set it.
        conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.detach()
        started = time.monotonic()
        waiting_logged = False
        while not conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": RANK_LOCK_KEY}).scalar():
            if ctx is not None:
                ctx.ensure_lease()
            waited = time.monotonic() - started
            if waited >= wait_s:
                raise RankLockTimeout(
                    f"the rank lock was held by another rank execution for "
                    f"{waited:.0f}s; giving up this attempt")
            if not waiting_logged:
                log.info("rank lock busy (another rank is running); waiting")
                waiting_logged = True
            time.sleep(RANK_LOCK_POLL_S)
        try:
            yield
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                             {"k": RANK_LOCK_KEY})
            except Exception as exc:                        # noqa: BLE001
                # Closing the connection below releases it regardless.
                log.warning("rank lock: explicit unlock failed (%s)", exc)
    finally:
        conn.close()


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
      running, lease expired
                    -> running, "stalled -- waiting for a worker to
                       reclaim" (its worker died or hung; the next claim
                       takes it over, or marks it dead on its last attempt)
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
    elif status == "running" and task.get("lease_expired"):
        detail = (f"Task #{tid} stalled — waiting for a worker to reclaim "
                  f"(attempt {n}/{m}, lease expired).")
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


def _write_ranked(ranked: list[dict]) -> None:
    """The one place RANKED_FILE is written (atomic replace). Everything
    that produces rankings -- the worker, the DB-less demo, the CLIs --
    ends here."""
    write_json_atomic(RANKED_FILE, ranked)


def rank_and_publish(profile: dict, jobs: list[dict]) -> list[dict]:
    """Rank `jobs` in this process (a full rank_jobs) and publish the result
    as RANKED_FILE. Returns the ranked list.

    For the callers that rank outside the queue: the DB-less Streamlit demo
    (it has no queue) and the CLIs (backend.main, which ranks its
    domain-filtered corpus, and job_scraper.run_real_ranking). With a
    database configured it holds the rank lock like every rank execution,
    so it never interleaves with a worker's."""
    with rank_lock() if queue_configured() else nullcontext():
        ranked = _rank_jobs(profile, jobs)
        _write_ranked(ranked)
    return ranked


def execute_rank(profile: dict, *, ctx: Any = None) -> dict:
    """One incremental rank execution against the Postgres corpus (strict
    mode: CorpusUnavailable instead of a stale JSON fallback), shared by
    the "rank" and "scan" handlers. Under the rank lock: read the corpus,
    score the delta, assemble, write RANKED_FILE. If the worker lost its
    lease (ctx.lease_lost) the output is NOT written: the task now belongs
    to another worker."""
    require_database()                  # fail fast, before waiting on a lock
    with rank_lock(ctx=ctx):
        corpus = load_corpus_rows()     # read under the lock: nothing missed
        outcome = scoring.rank_incrementally(corpus.rows, profile,
                                             make_scorer=_make_scorer, ctx=ctx)
        if ctx is not None:
            ctx.ensure_lease()
        _write_ranked(outcome.ranked)
    log.info("ranked %d jobs (%d scored, %d unchanged)", len(outcome.ranked),
             outcome.scored, outcome.skipped_unchanged)
    return {"ranked_count": len(outcome.ranked),
            "scored": outcome.scored,
            "skipped_unchanged": outcome.skipped_unchanged,
            "corpus_version": corpus_version_str(corpus.version),
            "profile_hash": outcome.profile_hash,
            "formula_version": outcome.formula_version}


def run_rank_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "rank" tasks: the profile on disk, then
    execute_rank(). Its result keeps the v1 keys (ranked_count,
    corpus_version, formula_version) and adds scored, skipped_unchanged
    and profile_hash."""
    return execute_rank(_load_profile(), ctx=ctx)
