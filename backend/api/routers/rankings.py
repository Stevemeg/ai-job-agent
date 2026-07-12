"""Ranking endpoints. Ranking 3,875 jobs takes minutes (embedding model),
so POST starts a background run and clients poll /rankings/status.

Single-process state is deliberate for the current single-user deployment;
the SaaS path swaps this for a real task queue (see docs/DEPLOYMENT.md)
without changing the endpoint contract."""
from __future__ import annotations

import json
import threading

from fastapi import APIRouter, HTTPException, Query

from ...config import RANKED_FILE
from ..deps import load_profile, load_jobs, load_ranked
from ..schemas import RankedJob, RankingStatus

router = APIRouter(prefix="/rankings", tags=["rankings"])

_state = {"state": "idle", "detail": "", "ranked_count": None}
_lock = threading.Lock()


def _run_ranking(profile: dict, jobs: list[dict]) -> None:
    global _state
    try:
        # Heavy import (sentence-transformers/torch) deferred to the worker
        # thread so the API process boots instantly without ML deps loaded.
        from ...matching_engine.matcher import rank_jobs
        ranked = rank_jobs(profile, jobs, candidate_is_fresher=True)
        with open(RANKED_FILE, "w", encoding="utf-8") as f:
            json.dump(ranked, f, ensure_ascii=False, indent=2)
        with _lock:
            _state = {"state": "done", "detail": "",
                      "ranked_count": len(ranked)}
    except Exception as exc:                                # noqa: BLE001
        with _lock:
            _state = {"state": "failed", "detail": str(exc),
                      "ranked_count": None}


@router.post("", response_model=RankingStatus, status_code=202)
def start_ranking():
    global _state
    with _lock:
        if _state["state"] == "running":
            raise HTTPException(409, "A ranking run is already in progress.")
        profile = load_profile()
        jobs = load_jobs()
        if not jobs:
            raise HTTPException(409, "No jobs.json. Run the collector first.")
        _state = {"state": "running",
                  "detail": f"Ranking {len(jobs)} jobs...",
                  "ranked_count": None}
    threading.Thread(target=_run_ranking, args=(profile, jobs),
                     daemon=True).start()
    return _state


@router.get("/status", response_model=RankingStatus)
def ranking_status():
    with _lock:
        if _state["state"] == "idle" and RANKED_FILE.exists():
            return {"state": "done", "detail": "loaded from previous run",
                    "ranked_count": None}
        return dict(_state)


@router.get("", response_model=list[RankedJob])
def get_rankings(limit: int = Query(50, ge=1, le=500),
                 offset: int = Query(0, ge=0),
                 min_score: float = Query(0.0, ge=0.0)):
    ranked = load_ranked()
    if min_score:
        ranked = [r for r in ranked if r["match_score"] >= min_score]
    return ranked[offset:offset + limit]
