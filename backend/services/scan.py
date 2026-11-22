"""
scan.py -- the scheduled incremental scan (Phase 3, ADR-001 §3/§4).

A "scan" task, run by the worker:

  collect from the ATS sources (each one isolated) -> upsert_jobs (commits)
  -> ranking.execute_rank(): take the rank lock -> score the delta ->
     assemble -> write RANKED_FILE -> release

The delta is computed inside execute_rank, AFTER the upsert has committed
and AFTER the rank lock is held, from job_scores alone -- never from what
this scan changed. So a retry (the worker died after the upsert) still
scores the jobs the first attempt saved, and jobs another collector saved
while a rank was running are picked up as well.

A failing source is recorded in the result and the scan carries on; the
task fails (and retries with backoff) only if EVERY source failed. Sources
are the existing public ATS APIs only (ADR-001 guardrail 3).

Enqueued by the scheduler (python -m backend.scheduler, idempotency key
from the scheduled fire time) and by POST /v1/scans. One scan at a time:
every scan task holds concurrency_key "scan:default" while queued or
running. Importing this module is cheap (no HTTP client, no ML stack): the
scheduler imports it.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import ranking

log = logging.getLogger(__name__)

SCAN_KIND = "scan"
SCAN_CONCURRENCY_KEY = "scan:default"
SCAN_MAX_ATTEMPTS = 3


class AllSourcesFailed(RuntimeError):
    """Every ATS source failed: nothing was collected, so the attempt fails
    (and is retried with backoff)."""


def _all_failed_message(failed: list[dict]) -> str:
    shown = "; ".join(f"{f['source']}/{f['slug']}: {f['error']}"
                      for f in failed[:5])
    more = f"; and {len(failed) - 5} more" if len(failed) > 5 else ""
    return f"all {len(failed)} ATS sources failed ({shown}{more})"


def run_scan_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "scan" tasks; returns the scan's stats.

    With no candidate profile yet, the corpus is still refreshed and the
    rank step is skipped (result: rank_skipped, scored 0, profile_hash
    None) -- the next scan or rank after a resume upload scores it all."""
    from ..database.corpus import load_corpus_snapshot, require_database, upsert_jobs
    from ..job_scraper import ats_collector
    from ..version import FORMULA_VERSION

    started = time.monotonic()
    require_database()                       # a scan never falls back to JSON
    collection = ats_collector.collect_sources()
    if not collection.sources_ok:
        raise AllSourcesFailed(_all_failed_message(collection.sources_failed))
    if ctx is not None:
        ctx.ensure_lease()                   # don't write for a lost task
    upserted = upsert_jobs(collection.jobs)  # committed before the rank lock

    profile = ranking.load_profile_if_present()
    if profile is not None:
        rank = ranking.execute_rank(profile, ctx=ctx)
        rank_skipped = None
    else:
        rank = {"ranked_count": None, "scored": 0, "skipped_unchanged": 0,
                "corpus_version": ranking.corpus_version_str(
                    load_corpus_snapshot(require_db=True).version),
                "profile_hash": None, "formula_version": FORMULA_VERSION}
        rank_skipped = ("no candidate profile yet -- upload a resume "
                        "(POST /v1/resumes); the next scan or rank scores the corpus")
        log.warning("scan: %s", rank_skipped)

    result = {
        "inserted": upserted["inserted"],
        "updated": upserted["updated"],
        "changed": upserted["changed"],
        "scored": rank["scored"],
        "skipped_unchanged": rank["skipped_unchanged"],
        "sources_ok": len(collection.sources_ok),
        "sources_failed": collection.sources_failed,
        "duration_s": round(time.monotonic() - started, 2),
        "corpus_version": rank["corpus_version"],
        "profile_hash": rank["profile_hash"],
        "formula_version": rank["formula_version"],
        "ranked_count": rank["ranked_count"],
    }
    if rank_skipped:
        result["rank_skipped"] = rank_skipped
    log.info("scan: %d new, %d re-sighted (%d changed), %d scored, %d unchanged, "
             "%d/%d sources ok, %.1fs", result["inserted"], result["updated"],
             result["changed"], result["scored"], result["skipped_unchanged"],
             result["sources_ok"],
             result["sources_ok"] + len(result["sources_failed"]),
             result["duration_s"])
    return result


def request_scan(payload: dict | None = None, *,
                 idempotency_key: str | None = None, conn: Any = None) -> int:
    """Queue a scan; returns the task id. Raises taskqueue.ConcurrencyConflict
    while a scan is queued or running (unless idempotency_key repeats an
    earlier enqueue: that returns the earlier task's id, and nothing new
    runs)."""
    from .. import taskqueue
    return taskqueue.enqueue(SCAN_KIND, payload or {},
                             idempotency_key=idempotency_key,
                             concurrency_key=SCAN_CONCURRENCY_KEY,
                             max_attempts=SCAN_MAX_ATTEMPTS, conn=conn)


def latest_scan_task() -> dict | None:
    from .. import taskqueue
    return taskqueue.latest_task(kind=SCAN_KIND,
                                 concurrency_key=SCAN_CONCURRENCY_KEY)
