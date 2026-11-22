"""
core.py -- the Postgres task queue (ADR-001 §2) over the `tasks` table.

Life of a task:

    enqueue() --> queued --claim()--> running --complete()--> succeeded
                    ^                    |
                    |   fail(), attempts < max_attempts:
                    +---- requeued with run_after = now() + backoff
                                         |
                         fail(), attempts >= max_attempts --> dead
          lease expired on its final attempt, found by claim() --> dead

- Leases: claim() sets lease_expires_at = now() + lease_s; the worker
  heartbeats to extend it. A running task whose lease has expired is
  claimable again: that is how a crashed or killed worker's task is
  recovered.
- attempts is incremented AT CLAIM, so a worker that dies mid-task still
  used up an attempt, and a task that keeps killing its workers cannot loop
  forever.
- Fencing: heartbeat / complete / fail only touch a row that is still
  (id, locked_by = worker_id, status = 'running'). Once another worker has
  reclaimed the task, the old holder's calls return False and change nothing.
- All timestamps come from the database clock (now()), never the worker's.

Every function here runs its own short transaction except enqueue(conn=...),
which joins the caller's (ADR-001: enqueue in the same transaction as the
domain write that caused it).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import TIMESTAMP, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from ..database.db import TASK_CONCURRENCY_INDEX, get_engine, init_db

log = logging.getLogger(__name__)

# Retry delay after a failed attempt n: min(CAP, BASE * 2**(n-1)), scaled by
# a random factor in [0.5, 1.0) ("equal jitter") so retries spread out.
BACKOFF_BASE_S = 5.0
BACKOFF_CAP_S = 300.0
MAX_ERROR_CHARS = 8000


class TaskQueueError(RuntimeError):
    """Base class for queue errors."""


class ConcurrencyConflict(TaskQueueError):
    """enqueue() with a concurrency_key that a queued or running task
    already holds (the partial unique index rejected the insert)."""

    def __init__(self, concurrency_key: str):
        super().__init__(f"a task holding concurrency_key {concurrency_key!r} "
                         "is already queued or running")
        self.concurrency_key = concurrency_key


@dataclass(frozen=True)
class ClaimedTask:
    """A task this worker holds the lease on (status 'running')."""
    id: int
    kind: str
    payload: dict
    attempts: int                # including this one
    max_attempts: int
    worker_id: str
    lease_expires_at: datetime
    idempotency_key: str | None = None
    concurrency_key: str | None = None


# --------------------------------------------------------------------------
# enqueue
# --------------------------------------------------------------------------
_INSERT = text("""
    INSERT INTO tasks (kind, payload, idempotency_key, concurrency_key,
                       run_after, max_attempts)
    VALUES (:kind, :payload, :idempotency_key, :concurrency_key,
            COALESCE(:run_after, now()), :max_attempts)
    ON CONFLICT (idempotency_key) DO NOTHING
    RETURNING id
""").bindparams(bindparam("payload", type_=JSONB),
                bindparam("run_after", type_=TIMESTAMP(timezone=True)))

_BY_IDEMPOTENCY_KEY = text("SELECT id FROM tasks WHERE idempotency_key = :k")


def _violated_constraint(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)


def _enqueue(conn: Any, params: dict) -> int:
    try:
        # Savepoint: a conflict must not abort a transaction we joined.
        with conn.begin_nested():
            task_id = conn.execute(_INSERT, params).scalar()
    except IntegrityError as exc:
        if _violated_constraint(exc) == TASK_CONCURRENCY_INDEX:
            raise ConcurrencyConflict(params["concurrency_key"]) from exc
        raise
    if task_id is None:
        # ON CONFLICT (idempotency_key) DO NOTHING: the key was used before
        # (if by an uncommitted transaction, the insert waited for it to
        # commit). Hand back that task; nothing new will run.
        task_id = conn.execute(_BY_IDEMPOTENCY_KEY,
                               {"k": params["idempotency_key"]}).scalar_one()
    return task_id


def enqueue(kind: str, payload: dict | None = None, *,
            idempotency_key: str | None = None,
            concurrency_key: str | None = None,
            run_after: datetime | None = None,
            max_attempts: int = 3,
            conn: Any = None) -> int:
    """Queue a task and return its id.

    - idempotency_key: enqueueing the same key again returns the existing
      task's id (whatever its status) -- no error, no second row, no rerun.
    - concurrency_key: at most one queued/running task per key; raises
      ConcurrencyConflict while one is active. The idempotency check wins:
      a repeated key returns the existing task even while it is active.
    - run_after: earliest start (tz-aware datetime); default now().
    - conn: a SQLAlchemy Connection inside the caller's transaction. The
      task row then commits or rolls back with the caller's writes. The
      caller must have run db.init_db() before opening that transaction
      (it takes table locks, so it is never run from inside one here).
    """
    if not kind:
        raise ValueError("kind is required")
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    params = {"kind": kind, "payload": payload or {},
              "idempotency_key": idempotency_key,
              "concurrency_key": concurrency_key,
              "run_after": run_after, "max_attempts": max_attempts}
    if conn is not None:
        return _enqueue(conn, params)
    init_db()
    with get_engine().begin() as own:
        return _enqueue(own, params)


# --------------------------------------------------------------------------
# claim
# --------------------------------------------------------------------------
# One statement: pick the oldest claimable row, skipping rows other workers
# have locked, and take it. A row reclaimed after its lease expired on the
# final attempt is marked dead instead of run (SET expressions see the
# pre-update row, so every CASE tests the old attempts/status).
_CLAIM = text("""
    UPDATE tasks SET
        status = CASE WHEN attempts >= max_attempts
                      THEN 'dead' ELSE 'running' END,
        attempts = CASE WHEN attempts >= max_attempts
                        THEN attempts ELSE attempts + 1 END,
        locked_by = CASE WHEN attempts >= max_attempts
                         THEN locked_by ELSE :worker_id END,
        lease_expires_at = CASE WHEN attempts >= max_attempts THEN NULL
            ELSE now() + make_interval(secs => CAST(:lease_s AS double precision))
            END,
        started_at = CASE WHEN attempts >= max_attempts
                          THEN started_at ELSE now() END,
        finished_at = CASE WHEN attempts >= max_attempts
                           THEN now() ELSE NULL END,
        last_error = CASE WHEN status = 'running'
            THEN 'lease expired (worker ' || COALESCE(locked_by, '?')
                 || ', attempt ' || attempts || '/' || max_attempts || ')'
            ELSE last_error END,
        updated_at = now()
    WHERE id = (
        SELECT id FROM tasks
        WHERE kind = ANY(:kinds)
          AND ((status = 'queued' AND run_after <= now())
               OR (status = 'running' AND lease_expires_at < now()))
        ORDER BY run_after, id
        LIMIT 1
        FOR UPDATE SKIP LOCKED)
    RETURNING id, kind, payload, status, attempts, max_attempts,
              lease_expires_at, idempotency_key, concurrency_key, last_error
""")


def claim(worker_id: str, kinds: Iterable[str],
          lease_s: float) -> ClaimedTask | None:
    """Take the oldest claimable task of one of `kinds`, or None.

    Claimable: queued with run_after <= now(), or running with an expired
    lease. attempts is incremented here. Reclaimed tasks with no attempts
    left are marked dead (and the next candidate is tried)."""
    kinds = sorted(set(kinds))
    if not kinds:
        raise ValueError("claim() needs at least one task kind")
    if lease_s <= 0:
        raise ValueError("lease_s must be > 0")
    init_db()
    params = {"worker_id": worker_id, "kinds": kinds, "lease_s": float(lease_s)}
    while True:
        with get_engine().begin() as conn:
            row = conn.execute(_CLAIM, params).mappings().first()
        if row is None:
            return None
        if row["status"] == "dead":
            log.error("task %s (%s) is dead: %s, no attempts left",
                      row["id"], row["kind"], row["last_error"])
            continue
        return ClaimedTask(
            id=row["id"], kind=row["kind"], payload=row["payload"] or {},
            attempts=row["attempts"], max_attempts=row["max_attempts"],
            worker_id=worker_id, lease_expires_at=row["lease_expires_at"],
            idempotency_key=row["idempotency_key"],
            concurrency_key=row["concurrency_key"])


# --------------------------------------------------------------------------
# heartbeat / complete / fail -- all fenced on the lease holder
# --------------------------------------------------------------------------
_FENCE = "WHERE id = :id AND locked_by = :worker_id AND status = 'running'"

_HEARTBEAT = text(f"""
    UPDATE tasks SET
        lease_expires_at = now() + make_interval(
            secs => CAST(:lease_s AS double precision)),
        updated_at = now()
    {_FENCE}
    RETURNING id
""")

_COMPLETE = text(f"""
    UPDATE tasks SET
        status = 'succeeded', result = :result, finished_at = now(),
        lease_expires_at = NULL, updated_at = now()
    {_FENCE}
    RETURNING id
""").bindparams(bindparam("result", type_=JSONB(none_as_null=True)))

_FAIL = text(f"""
    UPDATE tasks SET
        status = CASE WHEN attempts < max_attempts THEN 'queued' ELSE 'dead' END,
        run_after = CASE WHEN attempts < max_attempts
            THEN now() + make_interval(secs =>
                 LEAST(CAST(:cap AS double precision),
                       CAST(:base AS double precision)
                       * power(2, GREATEST(attempts - 1, 0)))
                 * (0.5 + random() / 2))
            ELSE run_after END,
        locked_by = CASE WHEN attempts < max_attempts THEN NULL ELSE locked_by END,
        lease_expires_at = NULL,
        finished_at = CASE WHEN attempts < max_attempts THEN NULL ELSE now() END,
        last_error = :error,
        updated_at = now()
    {_FENCE}
    RETURNING status, attempts, max_attempts, run_after
""")


def _fenced(stmt: Any, params: dict) -> Any:
    with get_engine().begin() as conn:
        return conn.execute(stmt, params).mappings().first()


def _rejected(op: str, task_id: int, worker_id: str) -> bool:
    log.warning("task %s: %s() from %s rejected -- it no longer holds the "
                "lease (reclaimed by another worker, or already finished)",
                task_id, op, worker_id)
    return False


def heartbeat(task_id: int, worker_id: str, lease_s: float) -> bool:
    """Extend the lease to now() + lease_s. False if the lease was lost."""
    row = _fenced(_HEARTBEAT, {"id": task_id, "worker_id": worker_id,
                               "lease_s": float(lease_s)})
    return True if row is not None else _rejected("heartbeat", task_id, worker_id)


def complete(task_id: int, worker_id: str, result: dict | None = None) -> bool:
    """Mark the task succeeded with `result` (JSON-serializable). False if
    this worker no longer holds the lease -- nothing is changed then."""
    row = _fenced(_COMPLETE, {"id": task_id, "worker_id": worker_id,
                              "result": result})
    return True if row is not None else _rejected("complete", task_id, worker_id)


def fail(task_id: int, worker_id: str, error: str) -> bool:
    """Record a failed attempt. With attempts left the task goes back to
    'queued' with run_after = now() + capped exponential backoff with
    jitter; otherwise it is 'dead'. last_error is always recorded. False if
    this worker no longer holds the lease -- nothing is changed then."""
    error = str(error)
    if len(error) > MAX_ERROR_CHARS:
        error = error[:MAX_ERROR_CHARS - 15] + "\n...[truncated]"
    row = _fenced(_FAIL, {"id": task_id, "worker_id": worker_id,
                          "error": error, "base": BACKOFF_BASE_S,
                          "cap": BACKOFF_CAP_S})
    if row is None:
        return _rejected("fail", task_id, worker_id)
    if row["status"] == "dead":
        log.error("task %s dead after %d/%d attempts: %s", task_id,
                  row["attempts"], row["max_attempts"], error.splitlines()[0]
                  if error else "")
    else:
        log.warning("task %s failed (attempt %d/%d), retrying at %s", task_id,
                    row["attempts"], row["max_attempts"],
                    row["run_after"].isoformat())
    return True


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------
# Every read adds lease_expired: running, but the lease ran out (the worker
# died or hung) -- the next claim() will take it over. Computed with the
# database clock, like every other lease decision.
_READ = ("SELECT *, COALESCE(status = 'running' AND lease_expires_at < now(), "
         "false) AS lease_expired FROM tasks")


def get_task(task_id: int) -> dict | None:
    """The tasks row as a dict, plus the computed `lease_expired`."""
    init_db()
    with get_engine().connect() as conn:
        row = conn.execute(text(_READ + " WHERE id = :id"),
                           {"id": task_id}).mappings().first()
    return dict(row) if row is not None else None


def latest_task(*, kind: str | None = None,
                concurrency_key: str | None = None) -> dict | None:
    """The most recently enqueued task matching the filters (with
    `lease_expired`, as get_task), or None. For a concurrency_key this is
    the active task if there is one (a new task can't be enqueued for the
    key while one is active)."""
    init_db()
    where, params = [], {}
    if kind is not None:
        where.append("kind = :kind")
        params["kind"] = kind
    if concurrency_key is not None:
        where.append("concurrency_key = :ckey")
        params["ckey"] = concurrency_key
    sql = (_READ
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY id DESC LIMIT 1")
    with get_engine().connect() as conn:
        row = conn.execute(text(sql), params).mappings().first()
    return dict(row) if row is not None else None
