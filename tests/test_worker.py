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
from backend.database.corpus import CorpusUnavailable  # noqa: E402
from backend.services import ranking               # noqa: E402
from backend.worker import EXIT_NO_DB, Worker, make_worker_id  # noqa: E402

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


# ---- the "rank" handler (no DB; the DB-backed tests are in test_rank_lock_db.py) --

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
    monkeypatch.setattr(ranking, "_make_scorer", lambda p: pytest.fail("scored"))
    with pytest.raises(CorpusUnavailable) as err:
        ranking.run_rank_task({})
    assert err.value.reason == "not_configured"
    assert not (tmp_path / "ranked.json").exists()


def test_rank_handler_needs_a_profile(monkeypatch, tmp_path):
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
