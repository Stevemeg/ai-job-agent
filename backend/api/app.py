"""
FastAPI application factory.

Run from project root:
    uvicorn backend.api.app:app --reload
Interactive docs at http://localhost:8000/docs (auto-generated).

Versioning: everything under /v1 from day one. Score-formula changes bump
`formula_version` here so clients can detect scoring regime changes -- the
events table snapshots scores for the same reason.
"""
from __future__ import annotations

from fastapi import FastAPI

from ..version import __version__, FORMULA_VERSION
from .routers import analysis, profile, rankings, tracker

app = FastAPI(
    title="Universal AI Job Agent API",
    version=__version__,
    description=(
        "Public HTTP interface over the job agent's engines. Routers hold no "
        "business logic -- every endpoint maps 1:1 to a tested function in "
        "backend.analysis / matching_engine / database."),
)

app.include_router(profile.router, prefix="/v1")
app.include_router(analysis.router, prefix="/v1")
app.include_router(rankings.router, prefix="/v1")
app.include_router(tracker.router, prefix="/v1")


@app.get("/healthz", tags=["meta"])
def healthz():
    """Liveness probe + version info for clients."""
    return {"status": "ok", "version": __version__,
            "formula_version": FORMULA_VERSION}
