"""The learning-to-rank dataset (Phase 6, export only).

- GET /v1/ltr/export: the CSV that `python -m backend.ltr.export` writes --
  one labelled row per job with a decision or an outcome (columns and
  labels: backend/ltr/export.py, docs/API.md). Headers: X-Labelled-Outcomes
  (the row count) and X-Training-Minimum (50). 503 without a database.

A read, so no API_TOKEN, like every GET. It holds your decisions and the
jobs you applied to: keep the API off the public internet
(docs/DEPLOYMENT.md).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter(prefix="/ltr", tags=["ltr"])


@router.get("/export", response_class=Response,
            responses={200: {"content": {"text/csv": {}}}})
def export_outcomes():
    from ...database.corpus import db_configured
    from ...ltr import MIN_LABELLED_OUTCOMES
    from ...ltr.export import export_rows, to_csv
    if not db_configured():
        raise HTTPException(503, "The outcomes live in Postgres: set DATABASE_URL or DB_HOST.")
    try:
        rows = export_rows()
    except Exception as exc:                                # noqa: BLE001
        raise HTTPException(503, f"Database unavailable ({exc.__class__.__name__}).")
    return Response(content=to_csv(rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="outcomes.csv"',
                             "X-Labelled-Outcomes": str(len(rows)),
                             "X-Training-Minimum": str(MIN_LABELLED_OUTCOMES)})
