"""Agent endpoints (Phase 4, DRY RUN): start an agent run and read what it
drafted. Nothing here sends, uploads or applies; the files only download.

- POST /v1/agent/runs queues an "agent_run" (202): refresh the ranking,
  triage the best new jobs, dispatch one application per job. 409 while a
  run is queued or running, or with no candidate profile; 503 without a
  (reachable) database. AGENT_ENABLED only gates the runs scans chain --
  a manual run is always allowed.
- GET /v1/agent/runs/latest: the latest run's state, and once done, its
  result (what was triaged and dispatched).
- GET /v1/applications: the applications, newest first (?status= filters).
- GET /v1/applications/{id}: status, score, validator reports, artifacts.
- GET /v1/applications/{id}/artifacts/{kind}: download one file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from ...services import ranking
from ..schemas import AgentRunStatus, ApplicationDetail, ApplicationSummary

router = APIRouter(tags=["agent"])

NO_DB_DETAIL = (
    "The agent runs on the durable task queue, which needs a database. Set "
    "DATABASE_URL or DB_HOST and run the worker (python -m backend.worker).")

MEDIA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def _require_queue() -> None:
    if not ranking.queue_configured():
        raise HTTPException(503, NO_DB_DETAIL)


def _db_unavailable(exc: Exception) -> HTTPException:
    return HTTPException(503, f"Database unavailable ({exc.__class__.__name__}); "
                              "the agent's tables can't be reached.")


# ---- runs -------------------------------------------------------------------------

@router.post("/agent/runs", response_model=AgentRunStatus, status_code=202)
def start_agent_run():
    _require_queue()
    if ranking.load_profile_if_present() is None:
        raise HTTPException(409, "No candidate profile, so nothing to triage for. "
                                 "POST /v1/resumes first.")
    from sqlalchemy.exc import SQLAlchemyError
    from ...agent import run_graph
    from ...database.corpus import CorpusUnavailable, require_database
    from ...taskqueue import ConcurrencyConflict, get_task
    try:
        require_database()             # short-timeout probe, not a hang
        task = get_task(run_graph.request_agent_run(trigger="api"))
    except ConcurrencyConflict:
        raise HTTPException(409, "An agent run is already in progress.")
    except (CorpusUnavailable, SQLAlchemyError) as exc:
        raise _db_unavailable(exc)
    return run_graph.agent_run_status(task)


@router.get("/agent/runs/latest", response_model=AgentRunStatus)
def latest_agent_run():
    _require_queue()
    from sqlalchemy.exc import SQLAlchemyError
    from ...agent import run_graph
    try:
        return run_graph.agent_run_status(run_graph.latest_agent_run_task())
    except SQLAlchemyError as exc:
        raise _db_unavailable(exc)


# ---- applications -------------------------------------------------------------------

def _download_url(application_id: int, kind: str) -> str:
    return f"/v1/applications/{application_id}/artifacts/{kind}"


def _detail(app: dict) -> dict:
    from ...agent import store
    from ...services.task_status import describe
    artifacts = []
    reports = {}
    for a in store.get_artifacts(app["id"]):
        path = Path(a["path"])
        artifacts.append({"kind": a["kind"], "sha256": a["sha256"],
                          "size_bytes": path.stat().st_size if path.is_file() else None,
                          "created_at": a["created_at"],
                          "download_url": _download_url(app["id"], a["kind"]),
                          "report": a["report"]})
        reports[a["kind"]] = a["report"]
    task = store.application_task(app["id"])
    task_info = None
    if task is not None:
        state, detail = describe(task)
        task_info = {"task_id": task["id"], "state": state, "detail": detail,
                     "attempts": task["attempts"], "max_attempts": task["max_attempts"]}
    validator = {}
    if "resume_docx" in reports:
        validator["resume"] = reports["resume_docx"]
    if "cover_letter_txt" in reports or "cover_letter_docx" in reports:
        validator["cover_letter"] = reports.get("cover_letter_docx",
                                                reports.get("cover_letter_txt"))
    return {**app, "artifacts": artifacts, "validator": validator, "task": task_info}


@router.get("/applications", response_model=list[ApplicationSummary])
def list_applications(status: Optional[str] = Query(None),
                      limit: int = Query(50, ge=1, le=500),
                      offset: int = Query(0, ge=0)):
    _require_queue()
    from sqlalchemy.exc import SQLAlchemyError
    from ...agent import store
    from ...database.db import APPLICATION_STATUSES
    if status is not None and status not in APPLICATION_STATUSES:
        raise HTTPException(422, f"status must be one of {', '.join(APPLICATION_STATUSES)}")
    try:
        return store.list_applications(status=status, limit=limit, offset=offset)
    except SQLAlchemyError as exc:
        raise _db_unavailable(exc)


@router.get("/applications/{application_id}", response_model=ApplicationDetail)
def get_application(application_id: int):
    _require_queue()
    from sqlalchemy.exc import SQLAlchemyError
    from ...agent import store
    try:
        app = store.get_application(application_id)
        if app is None:
            raise HTTPException(404, f"No application {application_id}.")
        return _detail(app)
    except SQLAlchemyError as exc:
        raise _db_unavailable(exc)


@router.get("/applications/{application_id}/artifacts/{kind}")
def download_artifact(application_id: int, kind: str):
    _require_queue()
    from sqlalchemy.exc import SQLAlchemyError
    from ...agent import store
    from ...database.db import ARTIFACT_KINDS
    if kind not in ARTIFACT_KINDS:
        raise HTTPException(404, f"Unknown artifact kind {kind!r} (one of "
                                 f"{', '.join(ARTIFACT_KINDS)}).")
    try:
        if store.get_application(application_id) is None:
            raise HTTPException(404, f"No application {application_id}.")
        artifact = next((a for a in store.get_artifacts(application_id)
                         if a["kind"] == kind), None)
    except SQLAlchemyError as exc:
        raise _db_unavailable(exc)
    if artifact is None:
        raise HTTPException(404, f"Application {application_id} has no {kind} yet.")
    path = Path(artifact["path"])
    if not path.is_file():
        raise HTTPException(404, f"The {kind} file for application {application_id} "
                                 "is no longer on disk.")
    return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower(),
                                                         "application/octet-stream"),
                        filename=f"application-{application_id}-{path.name}",
                        headers={"X-Artifact-SHA256": artifact["sha256"]})
