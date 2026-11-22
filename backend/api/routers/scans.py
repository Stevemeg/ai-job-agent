"""Scan endpoints (Phase 3). A scan -- collect from the ATS sources, upsert
the corpus, re-rank only the new or changed jobs -- runs on the worker as a
"scan" task: the same task the scheduler (python -m backend.scheduler)
enqueues every SCAN_CRON.

- POST /v1/scans queues a scan now (202). While a scan is queued or running
  it is 409. With no database there is no queue, so it is 503 (as is a
  configured database that can't be reached).
- GET /v1/scans/latest: the latest scan task's state, detail and trigger,
  and once it is done, its stats.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...services import ranking, scan
from ..schemas import ScanStatus

router = APIRouter(prefix="/scans", tags=["scans"])

NO_DB_DETAIL = (
    "Scans run on the durable task queue, which needs a database. Set "
    "DATABASE_URL or DB_HOST and run the worker (python -m backend.worker); "
    "the scheduler (python -m backend.scheduler) queues them on a timer.")


def _db_unavailable(exc: Exception) -> HTTPException:
    return HTTPException(503, f"Database unavailable ({exc.__class__.__name__}); "
                              "the scan queue can't be reached.")


@router.post("", response_model=ScanStatus, status_code=202)
def start_scan():
    if not ranking.queue_configured():
        raise HTTPException(503, NO_DB_DETAIL)
    # Deferred: SQLAlchemy only loads once a scan is actually requested.
    from sqlalchemy.exc import SQLAlchemyError
    from ...database.corpus import CorpusUnavailable, require_database
    from ...taskqueue import ConcurrencyConflict, get_task
    try:
        require_database()             # short-timeout probe, not a hang
        task = get_task(scan.request_scan({"trigger": "api"}))
    except ConcurrencyConflict:
        raise HTTPException(409, "A scan is already in progress.")
    except (CorpusUnavailable, SQLAlchemyError) as exc:
        raise _db_unavailable(exc)
    return scan.scan_status(task)


@router.get("/latest", response_model=ScanStatus)
def latest_scan():
    if not ranking.queue_configured():
        raise HTTPException(503, NO_DB_DETAIL)
    from sqlalchemy.exc import SQLAlchemyError
    try:
        return scan.scan_status(scan.latest_scan_task())
    except SQLAlchemyError as exc:
        raise _db_unavailable(exc)
