"""
backend.scheduler -- the scan scheduler (ADR-001 §3).

    python -m backend.scheduler

APScheduler 3.x with a SQLAlchemyJobStore on the app's database, in
timezone Asia/Kolkata. It holds one job, "scan:scheduled" (cron SCAN_CRON,
default "0 9 * * *" = 09:00 IST = 03:30 UTC), and that job ONLY enqueues a
"scan" task -- the worker runs the scan. This process never collects,
ranks or loads the ML stack.

Exactly-once per slot:
- Idempotency: each run enqueues with idempotency_key
  "scan:scheduled:<scheduled fire time, UTC>". The key comes from the
  SCHEDULED fire time (FireTimeExecutor passes it in), not the wall clock
  at execution, so re-firing a slot after a restart or a crash -- before
  APScheduler recorded the next run time -- is a no-op.
- Misfires: coalesce=True, max_instances=1, misfire_grace_time =
  SCAN_MISFIRE_GRACE_S (default 3600). Down past a fire time and back
  within the grace window -> the slot runs once; later -> it is skipped;
  several missed slots -> one run. A restart keeps the stored job's
  next_run_time when the trigger is unchanged (add_job(replace_existing)
  would otherwise recompute it from "now" and silently drop the missed
  slot).
- One scan at a time: concurrency_key "scan:default". If a scan is already
  queued or running, the scheduled one is logged and skipped.

Leader election (APScheduler 3.x can't share a job store between
schedulers): the scheduler takes pg_try_advisory_lock on a dedicated
connection. Not leader -> standby, retrying every 15 s, with no APScheduler
started at all. A leader that loses that connection (so Postgres has
released the lock) exits non-zero so its container restarts; and before
processing due jobs it re-checks the lock, so a scheduler that lost it
fires nothing and leaves the stored next run time alone for whichever
scheduler is leader next.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

TIMEZONE = "Asia/Kolkata"
JOB_ID = "scan:scheduled"
JOBSTORE_TABLE = "apscheduler_jobs"
DEFAULT_SCAN_CRON = "0 9 * * *"
DEFAULT_MISFIRE_GRACE_S = 3600
LEADER_LOCK_KEY = 0x7363686C        # b"schl"
STANDBY_RETRY_S = 15.0
LEADER_CHECK_S = 5.0

EXIT_OK = 0
EXIT_NO_DB = 2                      # refused to start: no configured, reachable DB
EXIT_LEADERSHIP_LOST = 3            # lost the leader lock: restart, never two leaders
EXIT_BAD_CONFIG = 4                 # SCAN_CRON / SCAN_MISFIRE_GRACE_S invalid


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def build_trigger(cron: str):
    """The CronTrigger for a 5-field crontab, in Asia/Kolkata."""
    from apscheduler.triggers.cron import CronTrigger
    return CronTrigger.from_crontab(cron, timezone=TIMEZONE)


def settings_from_env(env: dict | None = None) -> tuple[str, int]:
    """(SCAN_CRON, SCAN_MISFIRE_GRACE_S), validated. Raises ValueError."""
    env = os.environ if env is None else env
    cron = (env.get("SCAN_CRON") or DEFAULT_SCAN_CRON).strip()
    build_trigger(cron)                              # ValueError if malformed
    raw = (env.get("SCAN_MISFIRE_GRACE_S") or str(DEFAULT_MISFIRE_GRACE_S)).strip()
    try:
        grace = int(raw)
    except ValueError:
        raise ValueError(
            f"SCAN_MISFIRE_GRACE_S must be whole seconds, got {raw!r}") from None
    if grace < 1:
        raise ValueError(f"SCAN_MISFIRE_GRACE_S must be >= 1, got {grace}")
    return cron, grace


# --------------------------------------------------------------------------
# the job: enqueue only
# --------------------------------------------------------------------------
def scan_idempotency_key(fire_time: datetime) -> str:
    """One key per scheduled slot: its fire time in UTC."""
    return "scan:scheduled:" + fire_time.astimezone(timezone.utc).isoformat()


def enqueue_scheduled_scan(*, scheduled_for: datetime | None = None) -> int | None:
    """The scheduled job. Enqueues a "scan" task for the slot and returns its
    id (the existing task's, if this slot was enqueued before); None if
    another scan is queued or running (logged, skipped). Never runs the
    scan here.

    scheduled_for is filled in by FireTimeExecutor (it defaults to None only
    because APScheduler insists keyword-only job arguments be optional);
    without it there is no slot to key on, so it refuses rather than fall
    back to the wall clock."""
    if scheduled_for is None:
        raise TypeError("enqueue_scheduled_scan needs scheduled_for, the run's "
                        "scheduled fire time (FireTimeExecutor passes it)")
    from ..services import scan
    from ..taskqueue import ConcurrencyConflict
    slot = scheduled_for.astimezone(timezone.utc).isoformat()
    try:
        task_id = scan.request_scan({"trigger": "schedule", "scheduled_for": slot},
                                    idempotency_key=scan_idempotency_key(scheduled_for))
    except ConcurrencyConflict:
        log.warning("scheduled scan for %s skipped: another scan is already "
                    "queued or running", slot)
        return None
    log.info("scheduled scan for %s -> task %s", slot, task_id)
    return task_id


def _executor_class():
    from apscheduler.executors.base import BaseExecutor, run_job

    class _AtFireTime:
        """A due job, called with scheduled_for=<this run's fire time>."""

        def __init__(self, job: Any, run_time: datetime):
            self._job = job
            self.kwargs = {**job.kwargs, "scheduled_for": run_time}

        def __getattr__(self, name: str) -> Any:
            return getattr(self._job, name)

        def __str__(self) -> str:
            return str(self._job)

    class FireTimeExecutor(BaseExecutor):
        """Runs a due job inline in the scheduler thread (it only enqueues:
        one INSERT), passing it the run's SCHEDULED fire time. APScheduler's
        own run_job() still applies misfire_grace_time: a run later than
        that is reported missed and not run. (The same synchronous pattern
        as APScheduler's DebugExecutor.)"""

        def _do_submit_job(self, job: Any, run_times: list) -> None:
            events = []
            for run_time in run_times:
                events += run_job(_AtFireTime(job, run_time),
                                  job._jobstore_alias, [run_time],
                                  self._logger.name)
            self._run_job_success(job.id, events)

    return FireTimeExecutor


def _scheduler_class():
    from apscheduler.schedulers.background import BackgroundScheduler

    class LeaderOnlyScheduler(BackgroundScheduler):
        """Processes due jobs only while still_leader() holds. APScheduler
        advances a job's stored next_run_time even when running it fails,
        so a scheduler that lost the lock must not get that far: it fires
        nothing, sleeps until woken, and reports the loss."""

        def __init__(self, *, still_leader: Callable[[], bool],
                     on_lost: Callable[[], None], **options: Any):
            self._still_leader = still_leader
            self._on_lost = on_lost
            super().__init__(**options)

        def _process_jobs(self):
            if not self._still_leader():
                self._on_lost()
                return None
            return super()._process_jobs()

    return LeaderOnlyScheduler


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------
class SchedulerService:
    """Leader election + APScheduler. run() blocks until stop() / a signal
    (EXIT_OK) or until leadership is lost (EXIT_LEADERSHIP_LOST)."""

    def __init__(self, *, cron: str = DEFAULT_SCAN_CRON,
                 misfire_grace_s: int = DEFAULT_MISFIRE_GRACE_S,
                 lock_key: int = LEADER_LOCK_KEY,
                 standby_retry_s: float = STANDBY_RETRY_S,
                 check_s: float = LEADER_CHECK_S):
        self.cron = cron
        self.trigger = build_trigger(cron)
        self.misfire_grace_s = int(misfire_grace_s)
        self.lock_key = int(lock_key)
        self.standby_retry_s = float(standby_retry_s)
        self.check_s = float(check_s)
        self.state = "starting"          # -> standby -> leader -> stopped | lost
        self.scheduler: Any = None
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._wake = threading.Event()   # stop() or lost leadership: act now
        self._leader_conn: Any = None
        self._leader_engine: Any = None
        self._conn_lock = threading.Lock()

    # ---- leadership ---------------------------------------------------------
    def _connect(self) -> tuple[Any, Any]:
        """A dedicated, autocommit connection (outside the app's pool) with
        TCP keepalives both ways, so a dead link is noticed -- and the lock
        released by Postgres -- within about half a minute."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.pool import NullPool
        from ..database.db import get_engine
        engine = create_engine(get_engine().url, poolclass=NullPool, connect_args={
            "connect_timeout": 10, "application_name": "ai-job-agent-scheduler",
            "keepalives": 1, "keepalives_idle": 10, "keepalives_interval": 5,
            "keepalives_count": 3})
        try:
            conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            for setting in ("tcp_keepalives_idle = 10",
                            "tcp_keepalives_interval = 5",
                            "tcp_keepalives_count = 3"):
                conn.execute(text(f"SET {setting}"))
        except BaseException:
            engine.dispose()
            raise
        return engine, conn

    def try_acquire(self) -> bool:
        """One attempt at the leader lock. True = this process is leader."""
        from sqlalchemy import text
        engine, conn = self._connect()
        try:
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"),
                               {"k": self.lock_key}).scalar()
        except BaseException:
            conn.close()
            engine.dispose()
            raise
        if not got:
            conn.close()
            engine.dispose()
            return False
        with self._conn_lock:
            self._leader_engine, self._leader_conn = engine, conn
        return True

    def still_leader(self) -> bool:
        """The leader connection is alive and its session holds the lock."""
        from sqlalchemy import text
        with self._conn_lock:
            if self._leader_conn is None:
                return False
            try:
                return bool(self._leader_conn.execute(text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = "
                    "'advisory' AND granted AND pid = pg_backend_pid() AND "
                    "classid::bigint = :hi AND objid::bigint = :lo AND objsubid = 1)"),
                    {"hi": self.lock_key >> 32,
                     "lo": self.lock_key & 0xFFFFFFFF}).scalar())
            except Exception as exc:                        # noqa: BLE001
                log.error("scheduler: leader connection failed (%s: %s)",
                          exc.__class__.__name__, exc)
                return False

    def _release(self) -> None:
        with self._conn_lock:
            conn, engine = self._leader_conn, self._leader_engine
            self._leader_conn = self._leader_engine = None
        if conn is not None:
            try:
                conn.close()                # ends the session: lock released
            except Exception:                               # noqa: BLE001
                pass
            engine.dispose()

    # ---- APScheduler --------------------------------------------------------
    def _start_scheduler(self) -> None:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
        from ..database.db import get_engine
        self.scheduler = _scheduler_class()(
            still_leader=self.still_leader, on_lost=self._on_lost,
            jobstores={"default": SQLAlchemyJobStore(engine=get_engine(),
                                                     tablename=JOBSTORE_TABLE)},
            executors={"default": _executor_class()()},
            job_defaults={"coalesce": True, "max_instances": 1,
                          "misfire_grace_time": self.misfire_grace_s},
            timezone=TIMEZONE)
        self.scheduler.start(paused=True)
        self._ensure_job()
        self.scheduler.resume()

    def _ensure_job(self) -> None:
        """(Re)define "scan:scheduled" from the current settings. With the
        same trigger, the stored next_run_time is kept, so a slot missed
        while no scheduler ran is still fired (within the grace window)."""
        existing = self.scheduler.get_job(JOB_ID)
        keep = {}
        if existing is not None and existing.next_run_time is not None:
            if repr(existing.trigger) == repr(self.trigger):
                keep["next_run_time"] = existing.next_run_time
            else:
                log.warning("scheduler: SCAN_CRON changed (%s -> %s); the next "
                            "run is recomputed from now", existing.trigger,
                            self.trigger)
        self.scheduler.add_job(
            enqueue_scheduled_scan, self.trigger, id=JOB_ID,
            name="scheduled scan", replace_existing=True, coalesce=True,
            max_instances=1, misfire_grace_time=self.misfire_grace_s, **keep)
        job = self.scheduler.get_job(JOB_ID)
        log.info("scheduler: %s cron=%r (%s), misfire grace %ds, next run %s",
                 JOB_ID, self.cron, TIMEZONE, self.misfire_grace_s,
                 job.next_run_time.isoformat() if job.next_run_time else "paused")

    def _shutdown_scheduler(self) -> None:
        if self.scheduler is not None and self.scheduler.running:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:                               # noqa: BLE001
                log.exception("scheduler: APScheduler shutdown failed")

    # ---- lifecycle ------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _on_lost(self) -> None:
        self._lost.set()
        self._wake.set()

    def _on_signal(self, signum, _frame) -> None:
        log.info("scheduler: %s -- shutting down", signal.Signals(signum).name)
        self.stop()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return                          # signals are main-thread only (tests)
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._on_signal)

    def run(self) -> int:
        self._install_signal_handlers()
        announced = False
        while not self._stop.is_set():
            try:
                if self.try_acquire():
                    break
                if not announced:
                    log.info("scheduler: standby -- another scheduler holds the "
                             "leader lock; retrying every %.0fs", self.standby_retry_s)
                    announced = True
            except Exception as exc:                        # noqa: BLE001
                log.warning("scheduler: can't reach the database for the leader "
                            "lock (%s); retrying", exc.__class__.__name__)
            self.state = "standby"
            self._stop.wait(self.standby_retry_s)
        if self._stop.is_set():
            self.state = "stopped"
            return EXIT_OK

        try:
            log.info("scheduler: leader")
            self._start_scheduler()
            self.state = "leader"
            while True:
                self._wake.wait(self.check_s)
                self._wake.clear()
                if self._stop.is_set():
                    self.state = "stopped"
                    return EXIT_OK
                if self._lost.is_set() or not self.still_leader():
                    self.state = "lost"
                    log.error("scheduler: lost the leader lock (its connection "
                              "is gone); exiting so the container restarts -- "
                              "never two leaders")
                    return EXIT_LEADERSHIP_LOST
        finally:
            self._shutdown_scheduler()
            self._release()


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------
def check_database() -> str | None:
    """Why the scheduler can't start, or None. Its job store and the queue
    both live in Postgres; there is nothing to fall back to."""
    from ..database.corpus import db_configured, probe_database
    if not db_configured():
        return ("no database configured. The scheduler needs Postgres: set "
                "DATABASE_URL, or DB_HOST (+ DB_PORT/DB_USER/DB_PASSWORD/DB_NAME).")
    exc = probe_database()
    if exc is not None:
        return (f"database configured but unreachable "
                f"({exc.__class__.__name__}: {str(exc).strip()})")
    try:
        from ..database.db import init_db
        init_db()                           # the tasks table it enqueues into
    except Exception as exc:                                # noqa: BLE001
        return (f"database schema setup failed "
                f"({exc.__class__.__name__}: {str(exc).strip()})")
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog="python -m backend.scheduler",
        description="Run the scan scheduler (enqueues scheduled scans; "
                    "one leader at a time).")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        cron, grace = settings_from_env()
    except ValueError as exc:
        print(f"scheduler: refusing to start -- {exc}", file=sys.stderr)
        return EXIT_BAD_CONFIG
    problem = check_database()
    if problem:
        print(f"scheduler: refusing to start -- {problem}", file=sys.stderr)
        return EXIT_NO_DB
    return SchedulerService(cron=cron, misfire_grace_s=grace).run()
