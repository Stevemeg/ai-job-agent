"""Ranking endpoints. Ranking 3,875 jobs takes minutes (embedding model),
so POST queues a run and clients poll /rankings/status -- the same contract
as v1, now backed by the durable Postgres task queue (ADR-001 §2) instead of
an in-process thread:

- POST /v1/rankings enqueues a "rank" task (202). The worker
  (python -m backend.worker) claims and runs it and is the only writer of
  ranked_jobs.json. While a rank task is queued or running, POST is 409.
  With no database configured there is no queue, so POST is 503.
- GET /v1/rankings/status maps the latest rank task onto RankingStatus
  (services.ranking.status_from_task). No state lives in this process, so
  restarts lose nothing and tests can't leak state into each other.
- GET /v1/rankings serves the ranked file, unchanged.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ...config import RANKED_FILE
from ...services import ranking
from ..deps import load_profile, load_ranked
from ..schemas import RankedJob, RankingStatus

router = APIRouter(prefix="/rankings", tags=["rankings"])

NO_QUEUE_DETAIL = (
    "Ranking runs on the durable task queue, which needs a database. Set "
    "DATABASE_URL or DB_HOST and run the worker (python -m backend.worker). "
    "Without a database, rank from the Streamlit app (demo mode ranks "
    "in-process).")


def _db_unavailable(exc: Exception) -> HTTPException:
    return HTTPException(503, f"Database unavailable ({exc.__class__.__name__}); "
                              "the ranking queue can't be reached.")


@router.post("", response_model=RankingStatus, status_code=202)
def start_ranking():
    if not ranking.queue_configured():
        raise HTTPException(503, NO_QUEUE_DETAIL)
    load_profile()                    # 404 before queueing work that can't run
    # Deferred: SQLAlchemy only loads once a ranking is actually requested.
    from sqlalchemy.exc import SQLAlchemyError
    from ...database.corpus import CorpusUnavailable
    from ...taskqueue import ConcurrencyConflict, get_task
    try:
        task = get_task(ranking.request_ranking())
    except ConcurrencyConflict:
        raise HTTPException(409, "A ranking run is already in progress.")
    except CorpusUnavailable as exc:
        if exc.reason == "empty":
            raise HTTPException(
                409, "No jobs in the database corpus. Import one first: "
                     "python -m backend.database.corpus import")
        raise _db_unavailable(exc)
    except SQLAlchemyError as exc:
        raise _db_unavailable(exc)
    return ranking.status_from_task(task, RANKED_FILE.exists())


@router.get("/status", response_model=RankingStatus)
def ranking_status():
    if not ranking.queue_configured():
        return ranking.status_from_task(None, RANKED_FILE.exists())
    from sqlalchemy.exc import SQLAlchemyError
    try:
        return ranking.ranking_status(RANKED_FILE.exists())
    except SQLAlchemyError as exc:
        raise _db_unavailable(exc)


@router.get("", response_model=list[RankedJob])
def get_rankings(limit: int = Query(50, ge=1, le=500),
                 offset: int = Query(0, ge=0),
                 min_score: float = Query(0.0, ge=0.0)):
    ranked = load_ranked()
    if min_score:
        ranked = [r for r in ranked if r["match_score"] >= min_score]
    return ranked[offset:offset + limit]
