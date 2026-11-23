"""
run_graph.py -- the agent run (Phase 4, DRY RUN): one LangGraph thread
"agent_run:<task_id>", run by the worker for an "agent_run" task.

  refresh_ranking -> triage -> dispatch -> END

- refresh_ranking: ranking.execute_rank() under the rank lock -- the same
  incremental rank the rank and scan tasks run, so it is cheap when nothing
  changed (every job_scores row current: zero scored). Fixes the profile
  hash and scoring cache key the rest of the run uses.
- triage: from job_scores for that profile + cache key, the best jobs the
  agent hasn't touched, in a location AGENT_LOCATIONS accepts and with a
  title AGENT_EXCLUDE_TITLE_WORDS doesn't exclude (store.triage,
  locations.py, titles.py; AGENT_* settings). A qualifying posting's
  apply_link is the application's.
- dispatch: per job, ONE transaction creates the applications row and
  enqueues its "application" task (idempotency key "application:<id>").
  A re-run (a retried task resumes here) creates no duplicates:
  UNIQUE (user_id, job_id) and ON CONFLICT DO NOTHING.

Enqueued by POST /v1/agent/runs and, when AGENT_ENABLED=true, by every scan
as its last step (idempotency key "agent_run:scan:<scan_task_id>"). One run
at a time: concurrency_key "agent_run:default". Nothing here sends,
uploads or applies anything.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from . import settings as agent_settings
from . import store

log = logging.getLogger(__name__)

AGENT_RUN_KIND = "agent_run"
AGENT_RUN_CONCURRENCY_KEY = "agent_run:default"
AGENT_RUN_MAX_ATTEMPTS = 3


def run_thread_id(task_id: int) -> str:
    return f"agent_run:{task_id}"


def scan_idempotency_key(scan_task_id: int) -> str:
    return f"agent_run:scan:{scan_task_id}"


class RunState(TypedDict, total=False):
    user_id: int
    profile_hash: str
    cache_key: str
    refresh: dict
    settings: dict
    candidates: list
    excluded: dict
    dispatched: list
    skipped_existing: int


def build_run_graph(ctx: Any = None):
    """The run graph's StateGraph builder; `ctx` is the worker's
    TaskContext (lease checks before writes), or None."""
    from langgraph.graph import END, START, StateGraph
    from ..services import ranking, scoring

    def refresh_ranking(state: RunState) -> dict:
        profile = ranking._load_profile()
        rank = ranking.execute_rank(profile, ctx=ctx)
        return {"user_id": store.user_for_profile(profile),
                "profile_hash": rank["profile_hash"],
                "cache_key": scoring.cache_key(),
                "refresh": {k: rank[k] for k in ("ranked_count", "scored",
                                                 "skipped_unchanged",
                                                 "corpus_version")}}

    def triage(state: RunState) -> dict:
        s = agent_settings.settings_from_env()
        excluded: dict = {}
        candidates = store.triage(
            user_id=state["user_id"], profile_hash=state["profile_hash"],
            cache_key=state["cache_key"], min_score=s.min_score,
            max_age_days=s.max_job_age_days, cap=s.max_drafts_per_run,
            policy=s.location_policy(), title_policy=s.title_policy(),
            stats=excluded)
        log.info("agent run: %d job(s) triaged (score >= %g, seen within %dd, "
                 "in %s%s, cap %d; left out: %s)", len(candidates), s.min_score,
                 s.max_job_age_days, "/".join(s.locations),
                 " or any remote" if s.accept_global_remote else "",
                 s.max_drafts_per_run,
                 ", ".join(f"{n} by {k}" for k, n in excluded.items()) or "none")
        return {"candidates": candidates, "settings": s.as_dict(),
                "excluded": excluded}

    def dispatch(state: RunState) -> dict:
        dispatched, skipped = [], 0
        for c in state["candidates"]:
            if ctx is not None:
                ctx.ensure_lease()
            made = store.dispatch(user_id=state["user_id"], job_id=c["job_id"],
                                  match_score=c["match_score"],
                                  profile_hash=state["profile_hash"],
                                  cache_key=state["cache_key"],
                                  posting=c.get("posting"))
            if made is None:
                skipped += 1               # already dispatched (a resumed run)
            else:
                dispatched.append({**made, "title": c["title"],
                                   "company": c["company"],
                                   "location": c.get("location")})
        return {"dispatched": dispatched, "skipped_existing": skipped}

    g = StateGraph(RunState)
    g.add_node("refresh_ranking", refresh_ranking)
    g.add_node("triage", triage)
    g.add_node("dispatch", dispatch)
    g.add_edge(START, "refresh_ranking")
    g.add_edge("refresh_ranking", "triage")
    g.add_edge("triage", "dispatch")
    g.add_edge("dispatch", END)
    return g


def run_agent_run_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "agent_run" tasks. The thread is keyed on the task
    id, so a retried task resumes its own run. Without a candidate profile
    there is nothing to triage for: the run completes as skipped."""
    from ..services import ranking
    from ..version import FORMULA_VERSION
    from .checkpoint import run_thread

    task_id = ctx.task.id if ctx is not None else int(payload["task_id"])
    base = {"trigger": payload.get("trigger"),
            "scan_task_id": payload.get("scan_task_id"),
            "formula_version": FORMULA_VERSION}
    if ranking.load_profile_if_present() is None:
        log.warning("agent run %s skipped: no candidate profile yet", task_id)
        return {**base, "skipped": "no candidate profile yet -- upload a resume "
                                   "(POST /v1/resumes)",
                "triaged": 0, "dispatched": [], "skipped_existing": 0}
    final = run_thread(build_run_graph(ctx), run_thread_id(task_id), {})
    result = {**base,
              "profile_hash": final["profile_hash"], "cache_key": final["cache_key"],
              "refresh": final["refresh"], "settings": final["settings"],
              "triaged": len(final["candidates"]),
              "excluded": final.get("excluded") or {},
              "dispatched": final["dispatched"],
              "skipped_existing": final["skipped_existing"]}
    log.info("agent run %s: %d triaged, %d dispatched, %d already had an "
             "application", task_id, result["triaged"], len(result["dispatched"]),
             result["skipped_existing"])
    return result


def request_agent_run(*, trigger: str, scan_task_id: int | None = None,
                      conn: Any = None) -> int:
    """Queue an agent run; returns the task id. From a scan, keyed on the
    scan's task id: a retried scan gets the same run back. Raises
    taskqueue.ConcurrencyConflict while another run is queued or running."""
    from .. import taskqueue
    payload = {"trigger": trigger}
    key = None
    if scan_task_id is not None:
        payload["scan_task_id"] = scan_task_id
        key = scan_idempotency_key(scan_task_id)
    return taskqueue.enqueue(AGENT_RUN_KIND, payload, idempotency_key=key,
                             concurrency_key=AGENT_RUN_CONCURRENCY_KEY,
                             max_attempts=AGENT_RUN_MAX_ATTEMPTS, conn=conn)


def latest_agent_run_task() -> dict | None:
    from .. import taskqueue
    return taskqueue.latest_task(kind=AGENT_RUN_KIND,
                                 concurrency_key=AGENT_RUN_CONCURRENCY_KEY)


def agent_run_status(task: dict | None) -> dict:
    """The AgentRunStatus payload (None: no run yet), shaped like ScanStatus."""
    from ..services.task_status import describe
    if task is None:
        return {"state": "idle", "detail": "No agent run has happened yet.",
                "task_id": None, "trigger": None, "scan_task_id": None,
                "attempts": 0, "max_attempts": None, "created_at": None,
                "started_at": None, "finished_at": None, "result": None}
    state, detail = describe(task)
    payload = task.get("payload") or {}
    return {"state": state, "detail": detail, "task_id": task["id"],
            "trigger": payload.get("trigger"),
            "scan_task_id": payload.get("scan_task_id"),
            "attempts": task["attempts"], "max_attempts": task["max_attempts"],
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "finished_at": task.get("finished_at"),
            "result": task.get("result") if state == "done" else None}
