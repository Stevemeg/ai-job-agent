"""Shared dependencies: profile/jobs loading with proper HTTP errors."""
from __future__ import annotations

import json

from fastapi import HTTPException

from ..config import PROFILE_FILE, JOBS_FILE, SAMPLE_JOBS_FILE, RANKED_FILE


def load_profile() -> dict:
    if not PROFILE_FILE.exists():
        raise HTTPException(404, "No profile found. POST /v1/resumes first.")
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_jobs() -> list[dict]:
    """The job corpus via the single corpus loader: Postgres when configured
    and reachable, else JOBS_FILE (gitignored, local/self-hosted), else the
    committed SAMPLE_JOBS_FILE so cloud deployments that never ran the
    collector still rank/browse out of the box. The JSON path stays cached
    on (path, mtime). Paths are read at call time so tests can monkeypatch
    them on this module."""
    from ..database.corpus import load_corpus
    return load_corpus(jobs_file=JOBS_FILE, sample_file=SAMPLE_JOBS_FILE)


def load_ranked() -> list[dict]:
    if not RANKED_FILE.exists():
        raise HTTPException(
            404, "No rankings yet. POST /v1/rankings to start a ranking run.")
    with open(RANKED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
