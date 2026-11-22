"""
ranking.py -- ranking as a queued task (ADR-001 §2).

The one ranking service shared by the API (POST /v1/rankings), the Streamlit
UI and the worker, so data/ranked_jobs.json has a single writer: the worker
running a "rank" task (audit M7). The only in-process ranking left is the
DB-less Streamlit demo (ui/views/jobs.py), where there is no queue at all.

run_rank_task() is the worker handler for kind "rank":
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
