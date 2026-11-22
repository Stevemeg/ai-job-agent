"""Worker and rank-handler behaviour that needs no database."""
import json
import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")

from backend.database import corpus               # noqa: E402
from backend.database.corpus import CorpusSnapshot, CorpusUnavailable  # noqa: E402
from backend.services import ranking               # noqa: E402
from backend.worker import (EXIT_NO_DB, LeaseLost, TaskContext, Worker,  # noqa: E402
                            make_worker_id)

ROOT = Path(__file__).resolve().parents[1]


def _run_worker(env_overrides):
    env = {k: v for k, v in os.environ.items()
           if k not in ("DATABASE_URL", "DB_HOST", "DB_PORT")}
    env.update(env_overrides)
    return subprocess.run([sys.executable, "-m", "backend.worker"], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=60)


def test_worker_refuses_to_start_without_a_configured_db():
    proc = _run_worker({})
    assert proc.returncode == EXIT_NO_DB
    assert "refusing to start" in proc.stderr
    assert "DATABASE_URL" in proc.stderr and "DB_HOST" in proc.stderr


def test_worker_refuses_to_start_when_the_db_is_unreachable():
    pytest.importorskip("psycopg2")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
    proc = _run_worker({"DB_HOST": "127.0.0.1", "DB_PORT": str(closed),
                        "DB_CONNECT_TIMEOUT": "2"})
    assert proc.returncode == EXIT_NO_DB
    assert "unreachable" in proc.stderr


def test_worker_id_is_host_pid_short_uuid():
    host, pid, short = make_worker_id().rsplit(":", 2)
    assert host == socket.gethostname() and pid == str(os.getpid())
    assert re.fullmatch(r"[0-9a-f]{8}", short)
    assert make_worker_id() != make_worker_id()


def test_worker_rejects_an_empty_registry_and_bad_timings():
    with pytest.raises(ValueError):
        Worker({})
    with pytest.raises(ValueError):
        Worker({"x": lambda p, c: None}, lease_s=0)


def test_default_registry_is_keyed_by_task_kind():
    assert Worker().kinds == [ranking.RANK_KIND]


# ---- the "rank" handler ---------------------------------------------------------------

@pytest.fixture
def rank_env(tmp_path, monkeypatch, profile):
    profile_file = tmp_path / "candidate_profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    ranked_file = tmp_path / "ranked_jobs.json"
    monkeypatch.setattr(ranking, "PROFILE_FILE", profile_file)
    monkeypatch.setattr(ranking, "RANKED_FILE", ranked_file)
    calls = []

    def fake_snapshot(*args, **kwargs):
        calls.append(kwargs)
        return CorpusSnapshot(jobs=[{"title": "A"}, {"title": "B"}], source="db",
                              version=("db", 2, "2026-11-22T10:00:00+00:00"))

    monkeypatch.setattr(ranking, "load_corpus_snapshot", fake_snapshot)
    monkeypatch.setattr(ranking, "_rank_jobs", lambda p, jobs: [
        {"title": j["title"], "match_score": 50.0} for j in jobs])
    return ranked_file, calls


def test_rank_handler_uses_the_strict_corpus_and_writes_atomically(rank_env):
    ranked_file, calls = rank_env
    result = ranking.run_rank_task({})
    assert calls == [{"require_db": True}]
    from backend.version import FORMULA_VERSION
    assert result == {"ranked_count": 2,
                      "corpus_version": "db:2:2026-11-22T10:00:00+00:00",
                      "formula_version": FORMULA_VERSION}
    assert json.loads(ranked_file.read_text(encoding="utf-8"))[0]["title"] == "A"
    assert [p.name for p in ranked_file.parent.iterdir()
            if p.name.endswith(".tmp")] == []


def test_rank_handler_is_safe_to_rerun(rank_env):
    ranked_file, _ = rank_env
    first = ranking.run_rank_task({})
    content = ranked_file.read_bytes()
    assert ranking.run_rank_task({}) == first
    assert ranked_file.read_bytes() == content


def test_rank_handler_never_writes_after_losing_its_lease(rank_env):
    from backend.taskqueue import ClaimedTask
    ranked_file, _ = rank_env
    ctx = TaskContext(task=ClaimedTask(1, "rank", {}, 1, 3, "w", None),
                      worker_id="w", lease_lost=threading.Event())
    ctx.lease_lost.set()
    with pytest.raises(LeaseLost):
        ranking.run_rank_task({}, ctx)
    assert not ranked_file.exists()


def test_rank_handler_without_a_db_raises_instead_of_ranking_json(
        tmp_path, monkeypatch, profile):
    """No DB configured (conftest strips it): strict mode refuses even
    though a JSON corpus exists, so nothing is ranked or written."""
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([{"title": "From JSON"}]), encoding="utf-8")
    monkeypatch.setattr(corpus, "JOBS_FILE", jobs_file)
    profile_file = tmp_path / "p.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(ranking, "PROFILE_FILE", profile_file)
    monkeypatch.setattr(ranking, "RANKED_FILE", tmp_path / "ranked.json")
    monkeypatch.setattr(ranking, "_rank_jobs", lambda p, j: pytest.fail("ranked"))
    with pytest.raises(CorpusUnavailable):
        ranking.run_rank_task({})
    assert not (tmp_path / "ranked.json").exists()


def test_rank_handler_needs_a_profile(rank_env, monkeypatch, tmp_path):
    monkeypatch.setattr(ranking, "PROFILE_FILE", tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        ranking.run_rank_task({})


# ---- write_json_atomic under concurrent writers ----------------------------------------

def test_concurrent_atomic_writes_never_interleave(tmp_path):
    target = tmp_path / "ranked_jobs.json"
    docs = [[{"writer": w, "pad": "x" * 20000}] for w in range(4)]
    errors = []

    def write(doc):
        try:
            for _ in range(25):
                corpus.write_json_atomic(target, doc)
        except PermissionError:
            pass         # Windows: os.replace can't swap a file mid-replace
        except Exception as exc:                   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(d,)) for d in docs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert json.loads(target.read_text(encoding="utf-8")) in docs   # one whole doc
    assert [p.name for p in tmp_path.iterdir()] == ["ranked_jobs.json"]
