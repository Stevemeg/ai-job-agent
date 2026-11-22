"""services.ranking.status_from_task: the latest rank task -> the v1
RankingStatus contract (idle | running | done | failed). Pure, no DB."""
from backend.services.ranking import status_from_task


def _task(status, attempts=0, max_attempts=3, **extra):
    return {"id": 7, "status": status, "attempts": attempts,
            "max_attempts": max_attempts, "last_error": None, "result": None,
            **extra}


def test_no_task_keeps_the_previous_run_behaviour():
    assert status_from_task(None, ranked_file_exists=True) == {
        "state": "done", "detail": "loaded from previous run", "ranked_count": None}
    assert status_from_task(None, ranked_file_exists=False) == {
        "state": "idle", "detail": "", "ranked_count": None}


def test_queued_and_running_map_to_running_with_the_attempt():
    queued = status_from_task(_task("queued"), False)
    assert queued["state"] == "running"
    assert "queued" in queued["detail"] and "attempt 0/3" in queued["detail"]
    running = status_from_task(_task("running", attempts=2), False)
    assert running["state"] == "running" and "attempt 2/3" in running["detail"]


def test_queued_after_a_failure_is_running_retrying():
    s = status_from_task(_task("queued", attempts=1, last_error=(
        "ValueError: boom\n\nTraceback (most recent call last): ...")), False)
    assert s["state"] == "running"
    assert "retrying" in s["detail"] and "attempt 1/3" in s["detail"]
    assert "ValueError: boom" in s["detail"] and "Traceback" not in s["detail"]


def test_succeeded_is_done_with_the_ranked_count():
    s = status_from_task(_task("succeeded", attempts=1,
                               result={"ranked_count": 42}), True)
    assert s == {"state": "done", "detail": "", "ranked_count": 42}


def test_dead_is_failed_with_the_last_error():
    s = status_from_task(_task("dead", attempts=3, last_error=(
        "CorpusUnavailable: the database holds no collected jobs\n\nTraceback")),
        True)
    assert s == {"state": "failed", "ranked_count": None,
                 "detail": "CorpusUnavailable: the database holds no collected jobs"}
    lease = status_from_task(_task("dead", attempts=3, last_error=(
        "lease expired (worker h:1:ab, attempt 3/3)")), False)
    assert lease["detail"] == "lease expired (worker h:1:ab, attempt 3/3)"
