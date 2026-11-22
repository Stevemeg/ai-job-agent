"""
backend.taskqueue -- the durable Postgres work queue (ADR-001 §2).

Named `taskqueue`, not `queue`: a top-level `queue` would shadow the
standard library module of the same name.

    from backend import taskqueue

    task_id = taskqueue.enqueue("rank", {}, concurrency_key="rank:default")
    task = taskqueue.claim(worker_id, ["rank"], lease_s=60)
    ...
    taskqueue.complete(task.id, worker_id, {"ranked_count": 120})

The worker that drives this loop is backend/worker (python -m backend.worker).
"""
from .core import (
    BACKOFF_BASE_S,
    BACKOFF_CAP_S,
    ClaimedTask,
    ConcurrencyConflict,
    TaskQueueError,
    claim,
    complete,
    enqueue,
    fail,
    get_task,
    heartbeat,
    latest_task,
)

__all__ = [
    "BACKOFF_BASE_S", "BACKOFF_CAP_S", "ClaimedTask", "ConcurrencyConflict",
    "TaskQueueError", "claim", "complete", "enqueue", "fail", "get_task",
    "heartbeat", "latest_task",
]
