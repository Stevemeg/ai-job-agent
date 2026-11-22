# v2 Plan — "Always-On Agent"

Branch: `v2-always-on` · Architecture: [ADR-001](adr/ADR-001-v2-always-on-architecture.md) · Status tracked in [ROADMAP.md](ROADMAP.md)

v2 turns the request-driven v1 pipeline into a durable, scheduled agent that
stops for human approval before anything leaves the system. The guardrails in
ADR-001 apply to **every** phase. They are: never auto-submit, Gmail drafts
only, ATS APIs only, no CAPTCHA bypass, and the truthfulness validator is
mandatory on every generated document.

**Rule for every phase:** the full test suite stays green (baseline: 71 passed
on `main` @ `e5cb0df`), and each phase adds tests for what it introduces.

| # | Phase | Acceptance criteria (one line) | Status |
|---|---|---|---|
| 0 | Baseline | `v2-always-on` branched from `main`, pytest baseline recorded (71 passed), ADR-001 and this plan committed, and no files under `backend/` or `tests/` changed. | ✅ Done |
| 1 | Data layer | Collectors upsert into `canonical_jobs` (no duplicate rows on re-run), ranking reads the corpus from Postgres with the JSON fallback intact, and the `tasks` table plus new event types arrive through idempotent migrations. | ✅ Done |
| 2 | Durable worker | Ranking runs as a Postgres-queued task claimed via `SKIP LOCKED`; a worker killed mid-run is retried to exactly one completion, and `rankings.py` has no in-memory `_state`/thread while the `/v1/rankings` contract is unchanged. | ✅ Done |
| 3 | Scheduled incremental scan | APScheduler (SQLAlchemy job store, `Asia/Kolkata`) enqueues scans on schedule, a restart produces no duplicate or burst runs, and only jobs new or changed since the last scan are re-embedded and re-ranked. | 🔄 In review |
| 4 | Agent graph dry-run | The LangGraph graph (scan → rank → shortlist → generate → validate → approval) runs end to end with the Postgres checkpointer and **zero side effects**, and a run killed mid-graph resumes from its last checkpoint. | ⏳ |
| 5 | Approval + execution | The graph pauses at `interrupt()`; approve, edit or decline from the UI/API resumes it and auto-logs an `events` row; execution creates Gmail **drafts only**; and tests prove no code path can send email or submit an application. | ⏳ |
| 6 | Memory + follow-ups | Declined companies and roles are not resurfaced, and N days after an `applied` event with no recorded reply a validated follow-up draft is queued for approval. | ⏳ |
| 7 | Hardening + release | Guardrail test suite in CI, health checks and run logs for worker and scheduler, docs updated, CI green, and `v2.0.0` tagged from a reviewed merge to `main`. | ⏳ |

## Phase 3 — what shipped

- **Scheduler:** `python -m backend.scheduler` uses APScheduler 3.x with its job store in Postgres, timezone `Asia/Kolkata`, cron `SCAN_CRON` and misfire grace `SCAN_MISFIRE_GRACE_S`. It only enqueues `scan` tasks, keyed on the scheduled fire time. It elects a leader with a Postgres advisory lock, and a leader that loses the lock exits non-zero.
- **Scan task:** collect from each ATS board in isolation, upsert into `canonical_jobs`, then run an incremental rank. A failing board is reported in the result. Only an all-boards failure fails (and retries) the task.
- **Incremental ranking:** the new `job_scores` table caches the matcher's output per (job, profile hash, `FORMULA_VERSION`). A rank scores only jobs without a current row and reproduces a full `rank_jobs` exactly (parity tests compare the JSON). Every rank execution holds a session-level advisory lock for the scoring and the file write.
- **API:** `POST /v1/scans` and `GET /v1/scans/latest`. `/v1/rankings/status` now reports a stalled task, whose lease expired, as "stalled".

