"""Shared dependencies: profile/jobs loading with proper HTTP errors."""
from __future__ import annotations

import json

from fastapi import HTTPException

from ..config import PROFILE_FILE, JOBS_FILE, RANKED_FILE


def load_profile() -> dict:
    if not PROFILE_FILE.exists():
        raise HTTPException(404, "No profile found. POST /v1/resumes first.")
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# Parsing the ~3,875-job corpus per request is wasteful; cache on (path,
# mtime) so a corpus refresh invalidates automatically (audit fix). Path is
# read at call time so tests can monkeypatch JOBS_FILE on this module.
_jobs_cache: dict = {"key": None, "jobs": []}


def load_jobs() -> list[dict]:
    if not JOBS_FILE.exists():
        return []
    key = (str(JOBS_FILE), JOBS_FILE.stat().st_mtime)
    if _jobs_cache["key"] != key:
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            _jobs_cache["jobs"] = json.load(f)
        _jobs_cache["key"] = key
    return _jobs_cache["jobs"]


def load_ranked() -> list[dict]:
    if not RANKED_FILE.exists():
        raise HTTPException(
            404, "No rankings yet. POST /v1/rankings to start a ranking run.")
    with open(RANKED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)
