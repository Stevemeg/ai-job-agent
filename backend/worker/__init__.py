"""
backend.worker -- the durable worker (ADR-001 §2).

    python -m backend.worker [--kinds rank,scan] [--lease 60] [--grace 30]

Claims tasks from the Postgres queue (backend.taskqueue) and runs the
handler registered for each task's kind:

- worker_id = host:pid:short-uuid, unique per process.
- While a handler runs, a heartbeat thread extends the lease every
  lease/3 seconds. If a heartbeat is rejected (another worker reclaimed the
  task), TaskContext.lease_lost is set so the handler can stop before
  writing anything; its eventual complete()/fail() is rejected either way.
- Idle polling backs off exponentially from poll_min_s to poll_max_s and
  resets as soon as a task is found.
- SIGTERM / SIGINT: stop claiming, let the current task finish within the
  grace period, then exit. A task still running when the grace period ends
  is abandoned; its lease expires and another worker reclaims it (the
  attempt counts). A second signal exits immediately.

Handlers are `handler(payload: dict, ctx: TaskContext) -> dict | None`;
the return value is stored as the task's result. Raising fails the attempt
(retried with backoff until max_attempts, then dead).

Dead handlers, `dead_handler(payload: dict, last_error: str | None)`, run
once a task of their kind is dead: its final attempt raised, or a worker
found its lease expired with no attempts left.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

from .. import taskqueue

log = logging.getLogger(__name__)

DEFAULT_LEASE_S = 60.0
DEFAULT_GRACE_S = 30.0
DEFAULT_POLL_MIN_S = 0.5
DEFAULT_POLL_MAX_S = 10.0

EXIT_OK = 0
EXIT_NO_DB = 2              # refused to start: no configured, reachable DB
EXIT_GRACE_EXPIRED = 3      # stopped with a task unfinished (lease recovers it)

_TRACEBACK_CHARS = 4000


class LeaseLost(RuntimeError):
    """Raised by TaskContext.ensure_lease() once another worker owns the task."""


@dataclass
class TaskContext:
    task: taskqueue.ClaimedTask
    worker_id: str
    lease_lost: threading.Event

    def ensure_lease(self) -> None:
        """Call before side effects: raises LeaseLost if the lease was lost."""
        if self.lease_lost.is_set():
            raise LeaseLost(f"task {self.task.id}: lease lost; another worker "
                            "now owns it -- not writing results")


Handler = Callable[[dict, TaskContext], Optional[dict]]
DeadHandler = Callable[[dict, Optional[str]], None]


@dataclass(frozen=True)
class RunResult:
    task_id: int
    kind: str
    attempt: int
    outcome: str                 # "succeeded" | "failed" | "fenced"
    error: Optional[str] = None


def make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def default_handlers() -> dict[str, Handler]:
    from ..services.ranking import RANK_KIND, run_rank_task
    from ..services.scan import SCAN_KIND, run_scan_task
    return {RANK_KIND: run_rank_task, SCAN_KIND: run_scan_task}


def default_dead_handlers() -> dict[str, DeadHandler]:
    return {}


def _format_error(exc: BaseException) -> str:
    """'Type: message', then the traceback after a blank line (the status
    mapping shows only the first paragraph)."""
    summary = f"{exc.__class__.__name__}: {exc}"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(tb) > _TRACEBACK_CHARS:
        tb = "...\n" + tb[-_TRACEBACK_CHARS:]
    return f"{summary}\n\n{tb}"


class _Heartbeat(threading.Thread):
    def __init__(self, task_id: int, worker_id: str, lease_s: float,
                 lease_lost: threading.Event):
        super().__init__(name=f"heartbeat-task-{task_id}", daemon=True)
        self.task_id, self.worker_id, self.lease_s = task_id, worker_id, lease_s
        self.lease_lost = lease_lost
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.wait(self.lease_s / 3):
            try:
                ok = taskqueue.heartbeat(self.task_id, self.worker_id, self.lease_s)
            except Exception as exc:                        # noqa: BLE001
                # Transient DB trouble: keep trying; if it lasts past the
                # lease, the task is reclaimed and the next beat says so.
                log.warning("task %s: heartbeat failed (%s: %s)", self.task_id,
                            exc.__class__.__name__, exc)
                continue
            if not ok:
                self.lease_lost.set()
                return

    def stop(self) -> None:
        self._halt.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=10)


class Worker:
    def __init__(self, handlers: dict[str, Handler] | None = None, *,
                 worker_id: str | None = None,
                 lease_s: float = DEFAULT_LEASE_S,
                 grace_s: float = DEFAULT_GRACE_S,
                 poll_min_s: float = DEFAULT_POLL_MIN_S,
                 poll_max_s: float = DEFAULT_POLL_MAX_S,
                 dead_handlers: dict[str, DeadHandler] | None = None):
        self.handlers = dict(default_handlers() if handlers is None else handlers)
        if not self.handlers:
            raise ValueError("a worker needs at least one handler")
        # Default: the built-in dead handlers for the kinds this worker runs.
        dead = default_dead_handlers() if dead_handlers is None else dead_handlers
        self.dead_handlers = {k: h for k, h in dead.items() if k in self.handlers}
        if lease_s <= 0 or poll_min_s <= 0 or poll_max_s < poll_min_s:
            raise ValueError("need lease_s > 0 and 0 < poll_min_s <= poll_max_s")
        self.worker_id = worker_id or make_worker_id()
        self.lease_s = float(lease_s)
        self.grace_s = float(grace_s)
        self.poll_min_s = float(poll_min_s)
        self.poll_max_s = float(poll_max_s)
        self._stopping = threading.Event()
        self._force = threading.Event()
        self._loop_thread: threading.Thread | None = None

    @property
    def kinds(self) -> list[str]:
        return sorted(self.handlers)

    # ---- one task ---------------------------------------------------------
    def run_once(self) -> RunResult | None:
        """Claim one task and run it to completion in the calling thread
        (heartbeating meanwhile). None if nothing was claimable."""
        task = taskqueue.claim(self.worker_id, self.kinds, self.lease_s,
                               on_dead=self._on_dead_row)
        if task is None:
            return None
        return self._execute(task)

    def _on_dead(self, kind: str, payload: dict, error: str | None) -> None:
        handler = self.dead_handlers.get(kind)
        if handler is None:
            return
        try:
            handler(payload or {}, error)
        except Exception:                                   # noqa: BLE001
            log.exception("dead handler for %s failed", kind)

    def _on_dead_row(self, row: dict) -> None:
        # A task whose lease ran out on its final attempt (claim() marked it).
        self._on_dead(row["kind"], row.get("payload") or {}, row.get("last_error"))

    def _execute(self, task: taskqueue.ClaimedTask) -> RunResult:
        log.info("task %s (%s) claimed by %s, attempt %d/%d", task.id,
                 task.kind, self.worker_id, task.attempts, task.max_attempts)
        ctx = TaskContext(task=task, worker_id=self.worker_id,
                          lease_lost=threading.Event())
        beat = _Heartbeat(task.id, self.worker_id, self.lease_s, ctx.lease_lost)
        beat.start()
        result, error = None, None
        try:
            result = self.handlers[task.kind](task.payload, ctx)
            json.dumps(result)            # unserializable -> a failed attempt
        except Exception as exc:                            # noqa: BLE001
            error = _format_error(exc)
        finally:
            beat.stop()

        if error is None:
            ok = taskqueue.complete(task.id, self.worker_id, result)
            outcome = "succeeded" if ok else "fenced"
        else:
            log.warning("task %s (%s) attempt %d/%d raised %s", task.id,
                        task.kind, task.attempts, task.max_attempts,
                        error.split("\n", 1)[0])
            ok = taskqueue.fail(task.id, self.worker_id, error)
            outcome = "failed" if ok else "fenced"
            if ok and task.attempts >= task.max_attempts:   # fail() made it dead
                self._on_dead(task.kind, task.payload, error)
        if outcome == "succeeded":
            log.info("task %s (%s) succeeded", task.id, task.kind)
        return RunResult(task_id=task.id, kind=task.kind,
                         attempt=task.attempts, outcome=outcome, error=error)

    # ---- the loop -----------------------------------------------------------
    def request_stop(self) -> None:
        """Stop claiming; the current task (if any) runs on within grace_s."""
        self._stopping.set()

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def _loop(self) -> None:
        delay = self.poll_min_s
        while not self._stopping.is_set():
            try:
                ran = self.run_once()
            except Exception:                               # noqa: BLE001
                log.exception("worker %s: queue operation failed; backing off",
                              self.worker_id)
                ran = None
            if ran is not None:
                delay = self.poll_min_s
                continue
            # Idle (or DB trouble): sleep in short slices so a stop request
            # is noticed promptly, then back off exponentially.
            deadline = time.monotonic() + delay
            while not self._stopping.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._stopping.wait(min(left, 0.5))
            delay = min(delay * 2, self.poll_max_s)

    def _on_signal(self, signum, _frame) -> None:
        name = signal.Signals(signum).name
        if self._stopping.is_set():
            log.warning("worker %s: second %s, exiting now", self.worker_id, name)
            self._force.set()
            return
        log.info("worker %s: %s -- no new claims; finishing the current task "
                 "(grace %.0fs)", self.worker_id, name, self.grace_s)
        self._stopping.set()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return                       # signals are main-thread only (tests)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def run_forever(self) -> int:
        """Run until stopped (signal or request_stop()). Tasks execute on a
        loop thread so this (main) thread stays free to handle signals and
        enforce the grace period. Returns an EXIT_* code."""
        self._install_signal_handlers()
        loop = threading.Thread(target=self._loop, name="worker-loop", daemon=True)
        self._loop_thread = loop
        loop.start()
        deadline = None
        while loop.is_alive():
            loop.join(0.25)
            if not loop.is_alive():
                break
            if self._stopping.is_set() and deadline is None:
                deadline = time.monotonic() + self.grace_s
            if self._force.is_set() or (deadline is not None
                                        and time.monotonic() >= deadline):
                log.error("worker %s: exiting with a task unfinished; its "
                          "lease will expire and another worker will retry it",
                          self.worker_id)
                return EXIT_GRACE_EXPIRED
        log.info("worker %s stopped", self.worker_id)
        return EXIT_OK


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def check_database() -> str | None:
    """Why the worker can't start, or None. The worker is the queue's only
    consumer, so it refuses to run without a configured, reachable DB
    (there is no JSON fallback for a queue)."""
    from ..database.corpus import db_configured, probe_database
    if not db_configured():
        return ("no database configured. The worker needs Postgres: set "
                "DATABASE_URL, or DB_HOST (+ DB_PORT/DB_USER/DB_PASSWORD/"
                "DB_NAME).")
    exc = probe_database()
    if exc is not None:
        return (f"database configured but unreachable "
                f"({exc.__class__.__name__}: {str(exc).strip()})")
    try:
        from ..database.db import init_db
        init_db()
    except Exception as exc:                                # noqa: BLE001
        return (f"database schema setup failed "
                f"({exc.__class__.__name__}: {str(exc).strip()})")
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m backend.worker",
        description="Run the durable task worker (claims tasks from Postgres).")
    parser.add_argument("--kinds", default=None,
                        help="comma-separated task kinds to claim "
                             "(default: every registered kind)")
    parser.add_argument("--lease", type=float, default=DEFAULT_LEASE_S,
                        help="lease seconds; heartbeats every lease/3 "
                             f"(default {DEFAULT_LEASE_S:.0f})")
    parser.add_argument("--grace", type=float, default=DEFAULT_GRACE_S,
                        help="seconds a running task may take to finish "
                             f"after SIGTERM (default {DEFAULT_GRACE_S:.0f})")
    parser.add_argument("--poll-max", type=float, default=DEFAULT_POLL_MAX_S,
                        help="longest idle poll interval in seconds "
                             f"(default {DEFAULT_POLL_MAX_S:.0f})")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    problem = check_database()
    if problem:
        print(f"worker: refusing to start -- {problem}", file=sys.stderr)
        return EXIT_NO_DB

    handlers = default_handlers()
    if args.kinds:
        wanted = [k.strip() for k in args.kinds.split(",") if k.strip()]
        unknown = sorted(set(wanted) - set(handlers))
        if unknown:
            parser.error(f"no handler for kind(s): {', '.join(unknown)} "
                         f"(known: {', '.join(sorted(handlers))})")
        handlers = {k: handlers[k] for k in wanted}
    worker = Worker(handlers, lease_s=args.lease, grace_s=args.grace,
                    poll_max_s=max(args.poll_max, DEFAULT_POLL_MIN_S))
    log.info("worker %s started: kinds=%s lease=%.0fs grace=%.0fs",
             worker.worker_id, ",".join(worker.kinds), worker.lease_s,
             worker.grace_s)
    return worker.run_forever()
