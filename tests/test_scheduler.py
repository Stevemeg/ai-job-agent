"""The scan scheduler without a database: timezone, settings, the
fire-time idempotency key, and the executor that hands each run its
SCHEDULED fire time. The leader/misfire/restart tests that need Postgres
are in test_scheduler_db.py."""
import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("apscheduler")

from backend import scheduler                     # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


@pytest.mark.parametrize("now, expected", [
    (datetime(2026, 11, 22, 12, 0, tzinfo=UTC), datetime(2026, 11, 23, 3, 30, tzinfo=UTC)),
    (datetime(2026, 11, 22, 3, 0, tzinfo=UTC), datetime(2026, 11, 22, 3, 30, tzinfo=UTC)),
    (datetime(2026, 6, 1, 3, 30, 1, tzinfo=UTC), datetime(2026, 6, 2, 3, 30, tzinfo=UTC)),
])
def test_the_default_cron_fires_at_0330_utc(now, expected):
    trigger = scheduler.build_trigger(scheduler.DEFAULT_SCAN_CRON)
    fire = trigger.get_next_fire_time(None, now)
    assert str(fire.tzinfo) == "Asia/Kolkata"
    assert (fire.hour, fire.minute) == (9, 0)                 # 09:00 IST...
    assert fire.astimezone(UTC) == expected                   # ...is 03:30 UTC


def test_settings_come_from_the_environment_with_defaults():
    assert scheduler.settings_from_env({}) == ("0 9 * * *", 3600)
    assert scheduler.settings_from_env(
        {"SCAN_CRON": "30 6 * * 1-5", "SCAN_MISFIRE_GRACE_S": "600"}) == (
        "30 6 * * 1-5", 600)
    for bad in ({"SCAN_CRON": "every morning"}, {"SCAN_CRON": "0 9 * *"},
                {"SCAN_MISFIRE_GRACE_S": "1h"}, {"SCAN_MISFIRE_GRACE_S": "0"}):
        with pytest.raises(ValueError):
            scheduler.settings_from_env(bad)


def test_bad_settings_refuse_to_start(monkeypatch, capsys):
    monkeypatch.setenv("SCAN_CRON", "nonsense")
    assert scheduler.main([]) == scheduler.EXIT_BAD_CONFIG
    assert "refusing to start" in capsys.readouterr().err


def test_without_a_database_it_refuses_to_start():
    env = {k: v for k, v in __import__("os").environ.items()
           if k not in ("DATABASE_URL", "DB_HOST")}
    proc = subprocess.run([sys.executable, "-m", "backend.scheduler"], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode == scheduler.EXIT_NO_DB
    assert "refusing to start" in proc.stderr and "DATABASE_URL" in proc.stderr


def test_the_job_refuses_to_run_without_its_scheduled_fire_time():
    with pytest.raises(TypeError):
        scheduler.enqueue_scheduled_scan()


def test_the_idempotency_key_is_the_scheduled_slot_in_utc():
    ist = datetime(2026, 11, 23, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    utc = datetime(2026, 11, 23, 3, 30, tzinfo=UTC)
    assert scheduler.scan_idempotency_key(ist) == scheduler.scan_idempotency_key(utc) \
        == "scan:scheduled:2026-11-23T03:30:00+00:00"
    assert scheduler.scan_idempotency_key(utc + timedelta(days=1)) != \
        scheduler.scan_idempotency_key(utc)


def test_the_executor_passes_each_runs_scheduled_fire_time():
    """A run that starts late (here: 40 s after its slot, within the grace
    window) still gets its slot's time, not the wall clock."""
    from apscheduler.schedulers.background import BackgroundScheduler
    seen, fired = [], threading.Event()

    def job(*, scheduled_for=None):
        seen.append(scheduled_for)
        fired.set()

    sched = BackgroundScheduler(
        executors={"default": scheduler._executor_class()()},
        timezone=scheduler.TIMEZONE)
    slot = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=40)
    sched.add_job(job, scheduler.build_trigger("0 9 * * *"), id="j",
                  next_run_time=slot, misfire_grace_time=3600, coalesce=True)
    sched.start()
    try:
        assert fired.wait(10)
    finally:
        sched.shutdown(wait=False)
    assert seen == [slot]


def test_the_executor_keeps_apschedulers_misfire_rule():
    from apscheduler.events import EVENT_JOB_MISSED
    from apscheduler.schedulers.background import BackgroundScheduler
    ran, missed = [], threading.Event()
    sched = BackgroundScheduler(
        executors={"default": scheduler._executor_class()()},
        timezone=scheduler.TIMEZONE)
    sched.add_listener(lambda e: missed.set(), EVENT_JOB_MISSED)
    sched.add_job(lambda *, scheduled_for=None: ran.append(scheduled_for),
                  scheduler.build_trigger("0 9 * * *"), id="j",
                  next_run_time=datetime.now(UTC) - timedelta(minutes=10),
                  misfire_grace_time=60)
    sched.start()
    try:
        assert missed.wait(10)
    finally:
        sched.shutdown(wait=False)
    assert ran == []


_NO_ML_PROBE = textwrap.dedent("""
    import sys
    from datetime import datetime, timezone
    import backend.scheduler as s
    import backend.services.scan as scan
    calls = []
    scan.request_scan = lambda payload, idempotency_key=None: calls.append(
        (payload, idempotency_key)) or 41
    assert s.enqueue_scheduled_scan(
        scheduled_for=datetime(2026, 11, 23, 3, 30, tzinfo=timezone.utc)) == 41
    assert calls == [({"trigger": "schedule",
                       "scheduled_for": "2026-11-23T03:30:00+00:00"},
                      "scan:scheduled:2026-11-23T03:30:00+00:00")], calls
    s.SchedulerService(cron="0 9 * * *")
    heavy = sorted(m for m in sys.modules if m.split(".")[0] in (
        "torch", "sentence_transformers", "sklearn", "transformers")
        or m.startswith("backend.matching_engine"))
    assert heavy == [], heavy
    print("ok")
""")


def test_the_scheduler_never_imports_the_ml_stack():
    proc = subprocess.run([sys.executable, "-c", _NO_ML_PROBE], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
