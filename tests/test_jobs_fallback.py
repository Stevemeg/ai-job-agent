"""Deployed instances never have data/jobs.json (gitignored, local-only) --
load_jobs() must fall back to the committed data/sample_jobs.json so ranking
and the Jobs tab still work out of the box."""
import json

import pytest

fastapi = pytest.importorskip("fastapi")

from backend.api import deps                       # noqa: E402


def test_load_jobs_uses_full_corpus_when_present(tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"
    sample_file = tmp_path / "sample_jobs.json"
    jobs_file.write_text(json.dumps([{"title": "Real"}]), encoding="utf-8")
    sample_file.write_text(json.dumps([{"title": "Sample"}]), encoding="utf-8")

    monkeypatch.setattr(deps, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(deps, "SAMPLE_JOBS_FILE", sample_file)
    monkeypatch.setattr(deps, "_jobs_cache", {"key": None, "jobs": []})

    assert deps.load_jobs() == [{"title": "Real"}]


def test_load_jobs_falls_back_to_sample_when_full_corpus_missing(
        tmp_path, monkeypatch):
    jobs_file = tmp_path / "jobs.json"                # never created
    sample_file = tmp_path / "sample_jobs.json"
    sample_file.write_text(json.dumps([{"title": "Sample"}]), encoding="utf-8")

    monkeypatch.setattr(deps, "JOBS_FILE", jobs_file)
    monkeypatch.setattr(deps, "SAMPLE_JOBS_FILE", sample_file)
    monkeypatch.setattr(deps, "_jobs_cache", {"key": None, "jobs": []})

    assert deps.load_jobs() == [{"title": "Sample"}]


def test_load_jobs_empty_when_neither_file_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(deps, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(deps, "SAMPLE_JOBS_FILE", tmp_path / "sample_jobs.json")
    monkeypatch.setattr(deps, "_jobs_cache", {"key": None, "jobs": []})

    assert deps.load_jobs() == []
