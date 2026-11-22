"""
corpus.py -- the single source of the job corpus (ADR-001 §4).

canonical_jobs is the source of truth. Every reader (API, Streamlit UI, CLI
pipeline) goes through load_corpus(); collectors write through upsert_jobs()
(with no database, ats_collector.merge_into_jobs_file() dedupes into JSON
using the same dedupe_jobs() rules).

Read order -- load_corpus():
  1. Postgres, if a DB is CONFIGURED (DATABASE_URL or DB_HOST set) AND
     REACHABLE (short connect timeout, probed once per process) AND it holds
     collected rows (content_hash set; stub rows created by impression or
     outcome logging are not corpus).
  2. JOBS_FILE         data/jobs.json -- gitignored, local/self-hosted
  3. SAMPLE_JOBS_FILE  data/sample_jobs.json -- committed, for deployments
"Configured" must be explicit: a DB-less deployment (the Streamlit Cloud
demo) never attempts a connection at all, so its behaviour is unchanged.

Strict mode -- load_corpus_snapshot(require_db=True), used by the worker:
Postgres or nothing. Instead of falling back to JSON it raises
CorpusUnavailable (not configured, unreachable, read failed, or no collected
rows), and it re-probes on every call: a failure is never cached, so a
long-lived worker recovers as soon as the database does.

Every loader returns the dict shape matcher.rank_jobs consumes today (what
job_parser.parse_job / ats_collector._normalize produce). DB rows add
"postings".

Run from project root:
    python -m backend.database.corpus status
    python -m backend.database.corpus import [path]   # seed DB from a JSON corpus
    python -m backend.database.corpus export [path]   # DB corpus -> JSON

This module imports SQLAlchemy lazily, so the pure helpers (dedupe, hashing)
and the JSON path work without any DB dependency installed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..config import JOBS_FILE, SAMPLE_JOBS_FILE

log = logging.getLogger(__name__)

# Exactly the fields the matcher/analysis read -- the corpus dict shape.
JOB_FIELDS = ("title", "company", "location", "tags", "experience",
              "seniority", "clean_description", "apply_link", "source")

HASH_VERSION = "1"                 # bump if the content_hash recipe changes
DEFAULT_CONNECT_TIMEOUT_S = 3
UPSERT_PAGE_SIZE = 1000
# pg_advisory_xact_lock key serializing upsert_jobs() callers: postings are
# merged read-modify-write, so two collectors upserting the same job must
# not interleave. Transaction-scoped: released on commit/rollback.
_UPSERT_LOCK_KEY = 0x6A6F6273      # b"jobs"

_lock = threading.Lock()
_probe_result: bool | None = None  # DB reachability, cached per process
_json_cache: tuple | None = None   # ((path, mtime), jobs)
_db_cache: tuple | None = None     # (("db", count, max_last_seen), jobs)


class CorpusUnavailable(RuntimeError):
    """Strict mode (require_db=True) could not serve the corpus from
    Postgres. `reason` is one of: "not_configured", "unreachable",
    "read_failed", "empty"."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class CorpusSnapshot:
    """A corpus plus where it came from. `version` is hashable and changes
    whenever the corpus does (file mtime, or DB row count/last sighting), so
    callers can use it as a cache key."""
    jobs: list
    source: str                    # "db" | "jobs_file" | "sample" | "none"
    version: tuple
    path: Path | None = None


# --------------------------------------------------------------------------
# identity, postings, change detection (pure)
# --------------------------------------------------------------------------
def normalize_title(title: Any) -> str:
    """Must match db.CanonicalJob.normalize_title -- it is half of the
    UNIQUE(company, normalized_title) key (a test pins the equivalence)."""
    return " ".join((title or "").lower().split())


def job_key(job: dict) -> tuple[str, str]:
    """canonical_jobs identity: (stripped company, normalized title)."""
    return (job.get("company") or "").strip(), normalize_title(job.get("title"))


def _norm_text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _clean_postings(postings: Any) -> list[dict]:
    out = []
    for p in postings or []:
        if not isinstance(p, dict) or p.get("posting_id") in (None, ""):
            continue                      # can't dedupe what has no identity
        out.append({"source": p.get("source"),
                    "posting_id": str(p["posting_id"]),
                    "location": p.get("location"),
                    "apply_link": p.get("apply_link")})
    return out


def merge_postings(existing: Any, incoming: Any) -> list[dict]:
    """Union of two postings lists deduped on (source, posting_id). Keeps
    the order postings were first seen; the latest values win."""
    merged: dict[tuple, dict] = {}
    for p in _clean_postings(existing) + _clean_postings(incoming):
        merged[(p["source"], p["posting_id"])] = p   # re-assign keeps position
    return list(merged.values())


def location_set(job: dict, postings: list[dict]) -> list[str]:
    locations = {_norm_text(job.get("location")).casefold()}
    locations |= {_norm_text(p.get("location")).casefold() for p in postings}
    return sorted(loc for loc in locations if loc)


def content_hash(job: dict, postings: list[dict] | None = None) -> str:
    """sha256 over normalized title + description + location set.

    Whitespace and location case are normalized so cosmetic re-renders of a
    posting don't count as changes; apply links and tags are deliberately
    excluded (they churn without the job itself changing)."""
    if postings is None:
        postings = _clean_postings(job.get("postings"))
    parts = (HASH_VERSION, normalize_title(job.get("title")),
             _norm_text(job.get("clean_description")),
             "\x1e".join(location_set(job, postings)))
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def dedupe_jobs(jobs: Iterable[dict]) -> list[dict]:
    """Collapse jobs sharing a canonical key -- the same rule as
    canonical_jobs' UNIQUE constraint, so JSON and DB corpora agree.

    Position: where the key was FIRST seen (stable order for ranking ties).
    Fields:   the LATEST sighting wins (fresh descriptions replace stale).
    Postings: merged across sightings, deduped on (source, posting_id).
    """
    out: dict[tuple, dict] = {}
    for job in jobs:
        key = job_key(job)
        prev = out.get(key)
        merged = dict(job)
        if prev is not None and ("postings" in prev or "postings" in job):
            merged["postings"] = merge_postings(prev.get("postings"),
                                                job.get("postings"))
        elif "postings" in job:
            merged["postings"] = _clean_postings(job["postings"])
        out[key] = merged                            # re-assign keeps position
    return list(out.values())


# --------------------------------------------------------------------------
# database availability
# --------------------------------------------------------------------------
def db_configured() -> bool:
    """True only when a database was explicitly configured. db.py falls back
    to localhost dev defaults, so "unset" must not mean "try localhost" for
    the corpus -- that would make every DB-less deployment probe a port."""
    return bool(os.environ.get("DATABASE_URL") or os.environ.get("DB_HOST"))


def _connect_timeout() -> int:
    try:
        return max(1, int(os.environ.get("DB_CONNECT_TIMEOUT",
                                         DEFAULT_CONNECT_TIMEOUT_S)))
    except ValueError:
        return DEFAULT_CONNECT_TIMEOUT_S


def _probe_error() -> Exception | None:
    """One short-timeout connection attempt; the failure, or None if the
    database answered."""
    try:
        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import NullPool
        from .db import _build_db_url

        engine = create_engine(_build_db_url(), poolclass=NullPool,
                               connect_args={"connect_timeout": _connect_timeout()})
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
        return None
    except Exception as exc:                                # noqa: BLE001
        return exc


def probe_database() -> Exception | None:
    """Why a configured database can't be reached right now (short connect
    timeout), or None if it answered. Never cached -- for startup checks
    such as the worker's, not per-request use."""
    return _probe_error()


def _probe() -> bool:
    exc = _probe_error()
    if exc is not None:
        log.warning("corpus: database configured but unreachable (%s); "
                    "using the JSON corpus for this process",
                    exc.__class__.__name__)
    return exc is None


def db_available() -> bool:
    """Configured AND reachable. Probed once per process, then cached."""
    global _probe_result
    if _probe_result is None:
        with _lock:
            if _probe_result is None:
                _probe_result = db_configured() and _probe()
    return _probe_result


def _reset_caches() -> None:
    """Forget the probe result and cached corpora (tests, long-lived CLIs)."""
    global _probe_result, _json_cache, _db_cache
    with _lock:
        _probe_result = None
        _json_cache = None
        _db_cache = None


# --------------------------------------------------------------------------
# write side
# --------------------------------------------------------------------------
def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _row_for(job: dict) -> dict:
    company, ntitle = job_key(job)
    return {
        "company": company,
        "title": str(job.get("title")),
        "normalized_title": ntitle,
        "location": _text_or_none(job.get("location")),
        "tags": [str(t) for t in (job.get("tags") or [])],
        "experience": _text_or_none(job.get("experience")),
        "seniority": _text_or_none(job.get("seniority")),
        "clean_description": _text_or_none(job.get("clean_description")),
        "apply_link": _text_or_none(job.get("apply_link")),
        "source": _text_or_none(job.get("source")),
        "postings": _clean_postings(job.get("postings")),
    }


def upsert_jobs(jobs: Iterable[dict]) -> dict[str, int]:
    """Upsert jobs into canonical_jobs on (company, normalized_title) with
    INSERT ... ON CONFLICT DO UPDATE.

    - first_seen_at is set on insert only; last_seen_at bumps every sighting.
    - Scalar fields take the latest sighting; postings are merged with the
      stored list, deduped on (source, posting_id).
    - content_hash is recomputed; content_changed_at moves only when the
      hash differs (set on insert too: new content is a change).

    Returns {"inserted", "updated", "changed"}: updated counts every existing
    row sighted again; changed is the subset whose content_hash moved.
    """
    from sqlalchemy import case, func, literal_column, select, tuple_
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from .db import CanonicalJob, get_engine, init_db

    stats = {"inserted": 0, "updated": 0, "changed": 0}
    rows, skipped = [], 0
    for job in dedupe_jobs(jobs):
        company, ntitle = job_key(job)
        if not company or not ntitle:
            skipped += 1                  # can't be keyed; never invent one
            continue
        rows.append(_row_for(job))
    if skipped:
        log.warning("corpus: skipped %d job(s) without a company or title",
                    skipped)
    if not rows:
        return stats

    init_db()
    table = CanonicalJob.__table__
    with get_engine().begin() as conn:
        conn.execute(select(func.pg_advisory_xact_lock(_UPSERT_LOCK_KEY)))
        now = conn.execute(select(func.now())).scalar_one()   # txn timestamp

        stored: dict[tuple, Any] = {}
        keys = [(r["company"], r["normalized_title"]) for r in rows]
        for i in range(0, len(keys), UPSERT_PAGE_SIZE):
            page = keys[i:i + UPSERT_PAGE_SIZE]
            for company, ntitle, postings in conn.execute(
                    select(table.c.company, table.c.normalized_title,
                           table.c.postings)
                    .where(tuple_(table.c.company,
                                  table.c.normalized_title).in_(page))):
                stored[(company, ntitle)] = postings

        for r in rows:
            r["postings"] = merge_postings(
                stored.get((r["company"], r["normalized_title"])), r["postings"])
            r["content_hash"] = content_hash(r, r["postings"])
            r["content_changed_at"] = now

        stmt = pg_insert(table)
        new = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.company, table.c.normalized_title],
            set_={
                **{c: new[c] for c in (
                    "title", "location", "tags", "experience", "seniority",
                    "clean_description", "apply_link", "source", "postings",
                    "content_hash")},
                "content_changed_at": case(
                    (table.c.content_hash.is_distinct_from(new.content_hash),
                     new.content_changed_at),
                    else_=table.c.content_changed_at),
                "last_seen_at": func.now(),
            },
        ).returning(
            literal_column("(xmax = 0)").label("inserted"),
            (table.c.content_changed_at == func.now()).label("touched"),
        )
        for inserted, touched in conn.execute(stmt, rows):
            if inserted:
                stats["inserted"] += 1
            else:
                stats["updated"] += 1
                if touched:
                    stats["changed"] += 1
    return stats


# --------------------------------------------------------------------------
# read side
# --------------------------------------------------------------------------
def _row_to_job(row: Any) -> dict:
    job = {f: row[f] for f in JOB_FIELDS}
    job["tags"] = list(job["tags"] or [])
    job["postings"] = list(row["postings"] or [])
    return job


def _read_db_jobs(conn: Any) -> list[dict]:
    from sqlalchemy import select
    from .db import CanonicalJob
    t = CanonicalJob.__table__
    rows = conn.execute(
        select(*[t.c[f] for f in JOB_FIELDS], t.c.postings)
        .where(t.c.content_hash.isnot(None))
        .order_by(t.c.id)).mappings().all()
    return [_row_to_job(r) for r in rows]


def _db_snapshot(strict: bool = False) -> CorpusSnapshot | None:
    """The DB corpus, or None if it is empty or unreadable.

    Default mode: a read failure marks the DB unavailable for the rest of
    the process (the default engine has no connect timeout, so retrying per
    request could hang every one). Strict mode raises CorpusUnavailable
    instead and caches nothing -- its caller has just probed with a short
    timeout, and will probe again next call."""
    global _db_cache, _probe_result
    try:
        from sqlalchemy import func, select
        from .db import CanonicalJob, get_engine, init_db

        init_db()
        t = CanonicalJob.__table__
        with get_engine().connect() as conn:
            count, latest = conn.execute(
                select(func.count(), func.max(t.c.last_seen_at))
                .where(t.c.content_hash.isnot(None))).one()
            snap = None
            if count:
                key = ("db", count, latest)
                cached = _db_cache
                if cached is None or cached[0] != key:
                    cached = (key, _read_db_jobs(conn))
                    _db_cache = cached
                snap = CorpusSnapshot(jobs=cached[1], source="db", version=key)
    except Exception as exc:                                # noqa: BLE001
        if strict:
            raise CorpusUnavailable(
                "read_failed", f"database corpus read failed "
                f"({exc.__class__.__name__}: {exc})") from exc
        log.warning("corpus: database read failed (%s: %s); using the JSON "
                    "corpus for this process", exc.__class__.__name__, exc)
        with _lock:
            _probe_result = False
        return None
    if snap is None and strict:
        raise CorpusUnavailable(
            "empty", "the database holds no collected jobs -- seed it with "
            "`python -m backend.database.corpus import` or run the collector")
    return snap


def _strict_db_snapshot() -> CorpusSnapshot:
    if not db_configured():
        raise CorpusUnavailable(
            "not_configured", "no database configured (set DATABASE_URL or "
            "DB_HOST); strict corpus mode never falls back to JSON")
    exc = _probe_error()                  # every call: failures aren't cached
    if exc is not None:
        raise CorpusUnavailable(
            "unreachable", f"database configured but unreachable "
            f"({exc.__class__.__name__})") from exc
    return _db_snapshot(strict=True)


def _json_snapshot(jobs_file: Path, sample_file: Path) -> CorpusSnapshot:
    global _json_cache
    if jobs_file.exists():
        active, source = jobs_file, "jobs_file"
    elif sample_file.exists():
        active, source = sample_file, "sample"
    else:
        return CorpusSnapshot(jobs=[], source="none", version=("none",))
    # Parsing ~3,875 jobs per call is wasteful; cache on (path, mtime) so a
    # corpus refresh invalidates automatically.
    key = (str(active), active.stat().st_mtime)
    cached = _json_cache
    if cached is None or cached[0] != key:
        with open(active, "r", encoding="utf-8") as f:
            cached = (key, json.load(f))
        _json_cache = cached
    return CorpusSnapshot(jobs=cached[1], source=source,
                          version=("json",) + key, path=active)


def load_corpus_snapshot(jobs_file: Path | str | None = None,
                         sample_file: Path | str | None = None, *,
                         require_db: bool = False) -> CorpusSnapshot:
    """DB first, then jobs_file, then sample_file (defaults: config paths,
    read at call time so tests can monkeypatch them).

    require_db=True: Postgres only -- raises CorpusUnavailable rather than
    falling back, re-probing on every call; the file arguments are unused."""
    if require_db:
        return _strict_db_snapshot()
    if db_available():
        snap = _db_snapshot()
        if snap is not None:
            return snap
    return _json_snapshot(Path(jobs_file if jobs_file is not None else JOBS_FILE),
                          Path(sample_file if sample_file is not None
                               else SAMPLE_JOBS_FILE))


def load_corpus(jobs_file: Path | str | None = None,
                sample_file: Path | str | None = None) -> list[dict]:
    """The job corpus as a list of matcher-shaped dicts. Callers must treat
    it as read-only: it is cached and shared across calls."""
    return load_corpus_snapshot(jobs_file, sample_file).jobs


# --------------------------------------------------------------------------
# JSON export
# --------------------------------------------------------------------------
def write_json_atomic(path: Path | str, data: Any) -> None:
    """Write-then-rename so a concurrent reader never sees a half-written
    file. The temp name is unique per call, so two concurrent writers (two
    demo-mode reranks, or a worker that lost its lease racing its
    successor) can't interleave inside one temp file: each rename installs
    one complete document, and the last one wins. (Not tempfile.mkstemp:
    its 0600 mode would change the output file's permissions.)"""
    import uuid
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def export_corpus_json(path: Path | str | None = None) -> int:
    """Write the DB corpus (collected rows, load_corpus() shape) to `path`
    (default JOBS_FILE). Refuses to write an empty corpus over a file.
    Returns the number of jobs written."""
    from .db import get_engine, init_db
    init_db()
    with get_engine().connect() as conn:
        jobs = _read_db_jobs(conn)
    if not jobs:
        raise RuntimeError("The database holds no collected jobs; refusing to "
                           "overwrite the JSON corpus with an empty list.")
    write_json_atomic(path if path is not None else JOBS_FILE, jobs)
    return len(jobs)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m backend.database.corpus",
        description="Inspect, seed, or export the canonical job corpus.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="which corpus source is active, and its size")
    imp = sub.add_parser("import", help="upsert a JSON corpus into the database")
    imp.add_argument("path", nargs="?", default=str(JOBS_FILE))
    exp = sub.add_parser("export", help="write the database corpus to JSON")
    exp.add_argument("path", nargs="?", default=str(JOBS_FILE))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "status":
        snap = load_corpus_snapshot()
        print(f"database configured: {db_configured()}  reachable: {db_available()}")
        print(f"active corpus: {snap.source}"
              f"{' (' + str(snap.path) + ')' if snap.path else ''}"
              f" -- {len(snap.jobs):,} jobs")
        return 0

    if not db_available():
        print("No reachable database (set DB_HOST or DATABASE_URL).")
        return 1
    if args.cmd == "import":
        with open(args.path, "r", encoding="utf-8") as f:
            jobs = json.load(f)
        print(f"{upsert_jobs(jobs)} from {len(jobs):,} jobs in {args.path}")
    else:
        print(f"Wrote {export_corpus_json(args.path):,} jobs to {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
