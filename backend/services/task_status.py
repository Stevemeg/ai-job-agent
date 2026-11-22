"""
task_status.py -- a queue task's progress in words: the state/detail pair
behind both GET /v1/rankings/status and GET /v1/scans/latest.

  queued, never claimed   -> running, "Task #N queued, waiting for a worker (attempt 0/m)."
  queued after a failure  -> running, "Task #N retrying: attempt n/m failed (Type: message)."
  running, lease expired  -> running, "Task #N stalled — waiting for a worker to reclaim ..."
                             (its worker died or hung; the next claim takes it
                             over, or marks it dead on its last attempt)
  running                 -> running, "Task #N running (attempt n/m)."
  succeeded               -> done, ""
  dead (or 'failed')      -> failed, the last error's "Type: message"
"""
from __future__ import annotations


def error_summary(last_error: str | None) -> str:
    """The 'Type: message' paragraph of a worker-recorded error (the
    traceback after the blank line stays in the tasks row)."""
    return (last_error or "").split("\n\n", 1)[0].strip()


def describe(task: dict) -> tuple[str, str]:
    """(state, detail) for a tasks row (as taskqueue.get_task returns it)."""
    tid, n, m = task["id"], task["attempts"], task["max_attempts"]
    status = task["status"]
    if status == "queued" and n == 0:
        return "running", f"Task #{tid} queued, waiting for a worker (attempt 0/{m})."
    if status == "queued":
        return "running", (f"Task #{tid} retrying: attempt {n}/{m} failed "
                           f"({error_summary(task.get('last_error'))}).")
    if status == "running" and task.get("lease_expired"):
        return "running", (f"Task #{tid} stalled — waiting for a worker to "
                           f"reclaim (attempt {n}/{m}, lease expired).")
    if status == "running":
        return "running", f"Task #{tid} running (attempt {n}/{m})."
    if status == "succeeded":
        return "done", ""
    return "failed", (error_summary(task.get("last_error"))
                      or f"Task #{tid} failed after {n}/{m} attempts.")
