"""The scan scheduler against a real Postgres: the SQLAlchemy job store,
restarts that re-fire a slot, misfires, coalescing, and leader election.

Each test uses its own leader-lock key, and an hourly cron whose last slot
was ~2 minutes ago and whose next is ~58 minutes away, so nothing fires
unless a test makes it due. "Down past a fire time" is simulated by
writing the stored job's next_run_time, which is exactly what a scheduler
that was not running leaves behind."""
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("apscheduler")

from sqlalchemy import create_engine, text        # noqa: E402
from sqlalchemy.pool import NullPool              # noqa: E402

from backend import scheduler                     # noqa: E402
from backend.database import db                   # noqa: E402
from backend.services import scan                 # noqa: E402
# Bound at import, before the `enqueues` fixture swaps in its spy: a scan
# enqueued through this is not one of "the scheduler's" enqueues.
from backend.services.scan import request_scan as real_request_scan  # noqa: E402

pytestmark = pytest.mark.db

UTC = timezone.utc


def _wait(predicate, timeout=15.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {what}")


def _quiet_cron():
    minute = (datetime.now(ZoneInfo(scheduler.TIMEZONE)).minute - 2) % 60
    return f"{minute} * * * *"


def _last_slot(cron):
    """The most recent fire time of `cron` (~2-3 minutes ago)."""
    trigger, now = scheduler.build_trigger(cron), datetime.now(UTC)
    t = trigger.get_next_fire_time(None, now - timedelta(hours=1, minutes=5))
    last = None
    while t is not None and t <= now:
        last = t
        t = trigger.get_next_fire_time(t, t + timedelta(seconds=1))
    return last


class Running:
    """A SchedulerService.run() on a thread, with fast test timings."""

    def __init__(self, **options):
        options.setdefault("standby_retry_s", 0.2)
        options.setdefault("check_s", 0.2)
        self.service = scheduler.SchedulerService(**options)
        self.code = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        self.code = self.service.run()

    def start(self, state="leader"):
        self.thread.start()
        _wait(lambda: self.service.state == state or self.code is not None,
              what=f"scheduler {state}")
        assert self.service.state == state
        return self

    def stop(self):
        self.service.stop()
        self.thread.join(15)
        assert not self.thread.is_alive()
        return self.code

    def next_run(self):
        return self.service.scheduler.get_job(scheduler.JOB_ID).next_run_time


@pytest.fixture
def lock_key():
    return random.randint(1 << 24, (1 << 31) - 1)


@pytest.fixture
def enqueues(pg, monkeypatch):
    """Every idempotency key the scheduler's job enqueued with, in order."""
    keys, real = [], scan.request_scan

    def spy(payload=None, *, idempotency_key=None, conn=None):
        keys.append(idempotency_key)
        return real(payload, idempotency_key=idempotency_key, conn=conn)
    monkeypatch.setattr(scan, "request_scan", spy)
    db.init_db()
    return keys


def _scan_tasks(pg):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT id, idempotency_key, payload, status FROM tasks "
            "WHERE kind = 'scan' ORDER BY id")).all()


def _store():
    """The job store, opened without starting any scheduler. (A throwaway
    BackgroundScheduler won't do: on shutdown() its loop processes due jobs
    once more, which would fire -- and consume -- the slot under test.)"""
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.background import BackgroundScheduler
    store = SQLAlchemyJobStore(engine=db.get_engine(),
                               tablename=scheduler.JOBSTORE_TABLE)
    store.start(BackgroundScheduler(timezone=scheduler.TIMEZONE), "default")
    return store


def _stored_job():
    return _store().lookup_job(scheduler.JOB_ID)


def _set_next_run(when):
    """Make the stored job due at `when` -- as if no scheduler was running
    at that fire time (or one fired it and died before recording it)."""
    store = _store()
    job = store.lookup_job(scheduler.JOB_ID)
    job._modify(next_run_time=when)
    store.update_job(job)


def _seed(cron, lock_key):
    """Run a scheduler once so the job is in the store, then stop it."""
    assert Running(cron=cron, lock_key=lock_key).start().stop() == scheduler.EXIT_OK


# ---- the job in the store ------------------------------------------------------------

def test_the_job_is_stored_once_with_the_configured_settings(pg, lock_key, enqueues):
    _seed("0 9 * * *", lock_key)
    run = Running(cron="0 9 * * *", misfire_grace_s=1800, lock_key=lock_key).start()
    job = run.service.scheduler.get_job(scheduler.JOB_ID)
    assert (job.coalesce, job.max_instances, job.misfire_grace_time) == (True, 1, 1800)
    assert "timezone='Asia/Kolkata'" in repr(job.trigger)
    assert job.func_ref == "backend.scheduler:enqueue_scheduled_scan"
    assert job.next_run_time.astimezone(UTC).strftime("%H:%M") == "03:30"
    assert run.stop() == scheduler.EXIT_OK
    with pg.connect() as conn:
        assert conn.execute(text(
            f"SELECT count(*) FROM {scheduler.JOBSTORE_TABLE}")).scalar() == 1

    # A changed SCAN_CRON replaces the job (same id) with the new schedule.
    run = Running(cron="30 6 * * *", lock_key=lock_key).start()
    assert run.next_run().astimezone(UTC).strftime("%H:%M") == "01:00"
    run.stop()
    with pg.connect() as conn:
        assert conn.execute(text(
            f"SELECT count(*) FROM {scheduler.JOBSTORE_TABLE}")).scalar() == 1
    assert enqueues == []                           # nothing was due


# ---- one scan per slot ---------------------------------------------------------------

def test_a_restart_that_refires_the_same_slot_enqueues_one_scan(pg, lock_key, enqueues):
    cron = _quiet_cron()
    _seed(cron, lock_key)
    slot = _last_slot(cron)
    # The leader fired this slot, then died before APScheduler stored the
    # next run time, so the store still says `slot` is due.
    scheduler.enqueue_scheduled_scan(scheduled_for=slot)
    _set_next_run(slot)

    run = Running(cron=cron, lock_key=lock_key).start()     # the restart
    _wait(lambda: len(enqueues) == 2, what="the re-fire")
    _wait(lambda: run.next_run() > datetime.now(UTC), what="the next slot")
    run.stop()
    key = scheduler.scan_idempotency_key(slot)
    assert enqueues == [key, key]                           # fired twice...
    tasks = _scan_tasks(pg)
    assert [t.idempotency_key for t in tasks] == [key]      # ...one scan task
    assert tasks[0].payload == {"trigger": "schedule",
                                "scheduled_for": slot.astimezone(UTC).isoformat()}


def test_a_slot_missed_while_down_runs_once_within_the_grace_window(
        pg, lock_key, enqueues):
    cron = _quiet_cron()
    _seed(cron, lock_key)
    slot = _last_slot(cron)                                 # 2-3 minutes ago
    _set_next_run(slot)
    run = Running(cron=cron, misfire_grace_s=3600, lock_key=lock_key).start()
    _wait(lambda: len(_scan_tasks(pg)) == 1, what="the missed run")
    _wait(lambda: run.next_run() > datetime.now(UTC), what="the next slot")
    time.sleep(0.5)
    run.stop()
    assert enqueues == [scheduler.scan_idempotency_key(slot)]
    assert len(_scan_tasks(pg)) == 1


def test_a_slot_missed_beyond_the_grace_window_is_skipped(pg, lock_key, enqueues):
    cron = _quiet_cron()
    _seed(cron, lock_key)
    _set_next_run(_last_slot(cron))                         # 120-180 s ago
    run = Running(cron=cron, misfire_grace_s=60, lock_key=lock_key).start()
    _wait(lambda: run.next_run() > datetime.now(UTC), what="the slot to be passed")
    time.sleep(0.5)
    run.stop()
    assert enqueues == [] and _scan_tasks(pg) == []


def test_several_missed_slots_coalesce_into_one_run(pg, lock_key, enqueues):
    cron = _quiet_cron()
    _seed(cron, lock_key)
    slot = _last_slot(cron)
    _set_next_run(slot - timedelta(hours=3))                # 4 slots missed
    run = Running(cron=cron, misfire_grace_s=3600, lock_key=lock_key).start()
    _wait(lambda: len(_scan_tasks(pg)) == 1, what="the coalesced run")
    _wait(lambda: run.next_run() > datetime.now(UTC), what="the next slot")
    time.sleep(0.5)
    run.stop()
    assert enqueues == [scheduler.scan_idempotency_key(slot)]   # the latest only


def test_a_slot_that_finds_a_scan_running_is_skipped(pg, lock_key, enqueues):
    cron = _quiet_cron()
    _seed(cron, lock_key)
    # Enqueued past the spy, so `enqueues` holds only the scheduler's own
    # attempt (the manual one would record None and satisfy a length check
    # before the scheduler had fired at all).
    manual = real_request_scan({"trigger": "api"})          # holds scan:default
    slot = _last_slot(cron)
    _set_next_run(slot)
    run = Running(cron=cron, lock_key=lock_key).start()
    _wait(lambda: any((k or "").startswith("scan:scheduled:") for k in enqueues),
          what="the scheduled attempt")
    _wait(lambda: run.next_run() > datetime.now(UTC), what="the next slot")
    run.stop()
    assert enqueues == [scheduler.scan_idempotency_key(slot)]
    assert [t.id for t in _scan_tasks(pg)] == [manual]      # logged and skipped


# ---- leader election -------------------------------------------------------------------

def test_a_second_scheduler_stays_standby_then_takes_over(pg, lock_key, enqueues):
    cron = _quiet_cron()
    leader = Running(cron=cron, lock_key=lock_key).start()
    standby = Running(cron=cron, lock_key=lock_key).start(state="standby")
    time.sleep(0.8)                                         # several retries
    assert standby.service.state == "standby"
    assert standby.service.scheduler is None                # nothing that could fire

    slot = _last_slot(cron)
    leader.service.scheduler.modify_job(scheduler.JOB_ID, next_run_time=slot)
    _wait(lambda: len(_scan_tasks(pg)) == 1, what="the leader's run")
    assert enqueues == [scheduler.scan_idempotency_key(slot)]
    with pg.begin() as conn:                                # that scan finished
        conn.execute(text("UPDATE tasks SET status = 'succeeded'"))

    assert leader.stop() == scheduler.EXIT_OK               # releases the lock
    _wait(lambda: standby.service.state == "leader", what="the takeover")
    # (Anything due at or before `slot` would coalesce to `slot` again and
    # be a no-op; make a later, off-grid time due instead.)
    later = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=10)
    standby.service.scheduler.modify_job(scheduler.JOB_ID, next_run_time=later)
    _wait(lambda: len(_scan_tasks(pg)) == 2, what="the new leader's run")
    assert standby.stop() == scheduler.EXIT_OK
    assert enqueues == [scheduler.scan_idempotency_key(slot),
                        scheduler.scan_idempotency_key(later)]


def test_a_leader_that_loses_its_connection_fires_nothing_and_exits_nonzero(
        pg, lock_key, enqueues):
    cron = _quiet_cron()
    leader = Running(cron=cron, lock_key=lock_key, check_s=60).start()
    admin = create_engine(db._build_db_url(), poolclass=NullPool)
    with admin.connect() as conn:
        pids = conn.execute(text(
            "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND objid::bigint = :k"), {"k": lock_key}).scalars().all()
        assert len(pids) == 1
        conn.execute(text("SELECT pg_terminate_backend(:p)"), {"p": pids[0]})
    admin.dispose()

    slot = _last_slot(cron)                                 # a slot comes due...
    leader.service.scheduler.modify_job(scheduler.JOB_ID, next_run_time=slot)
    leader.thread.join(15)
    assert leader.code == scheduler.EXIT_LEADERSHIP_LOST    # ...it exits instead
    assert enqueues == [] and _scan_tasks(pg) == []
    assert _stored_job().next_run_time == slot              # slot left for the next leader

    successor = Running(cron=cron, lock_key=lock_key).start()
    _wait(lambda: len(_scan_tasks(pg)) == 1, what="the successor's run")
    successor.stop()
    assert enqueues == [scheduler.scan_idempotency_key(slot)]
