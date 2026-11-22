# ADR-001: v2 "Always-On Agent" architecture

- **Status:** Accepted
- **Date:** 2026-11-22
- **Scope:** v2 (branch `v2-always-on`), phases 0–7 in [V2_PLAN.md](../V2_PLAN.md)
- **Supersedes:** the "Auto-apply agent" direction under *Future* in [ROADMAP.md](../ROADMAP.md) (v2 never submits applications; see Guardrails)

## Context

v1.0 works, but only on request. Nothing happens unless a person starts it, and
none of the work in progress survives a restart.

| Concern | How v1 does it | Where | Limitation |
|---|---|---|---|
| Corpus refresh | Manual CLI: `python -m backend.job_scraper.ats_collector` | `job_scraper/ats_collector.py` | Nobody runs it on a schedule. `merge_into_jobs_file()` appends (`existing + new_jobs`) with no dedup, so duplicates pile up on every run and dedup is pushed downstream into `rank_jobs` |
| Corpus storage | `data/jobs.json` (3,875 jobs, gitignored), falling back to the committed `data/sample_jobs.json` (120 jobs) | `config.py`, `api/deps.py:load_jobs` | A JSON list has no stable identity. `canonical_jobs` exists in Postgres but is filled lazily, only when an impression or outcome is logged |
| Ranking runs | `POST /v1/rankings` starts a daemon `threading.Thread`; progress lives in the module-global `_state` dict behind a `threading.Lock` | `api/routers/rankings.py` | State is in memory and per process. A restart loses a running job. The Streamlit re-rank button in `ui/views/jobs.py` is a second, uncoordinated writer of `ranked_jobs.json` (audit **M7**). `_state` can leak between tests (audit **L5**) |
| Outcome logging | Manual: the interactive `input()` CLI `database/log_outcome_cli.py`, or the Tracker tab form, both calling `events.log_outcome()` | `database/events.py` | Outcomes are only recorded if the user remembers to log them. Learning-to-rank needs about 50 real outcomes, and manual logging is the bottleneck (ROADMAP "In Progress") |
| Document generation | Tailored resume and cover letter generated on demand, each checked by the deterministic truthfulness validator | `resume_engine/` | Synchronous and per click. No review queue, no follow-through |

The building blocks v2 needs are already in place: Postgres 16 is in the stack,
`canonical_jobs` and `events` have stable keys and score snapshots, the LLM is
behind a provider abstraction (`backend/llm/`), and the validator is
deterministic. What v1 lacks is a **durable control loop** that runs on its own,
survives restarts, and stops for a human before anything leaves the system.

## Decision

v2 runs as an always-on agent built from five durable components. All of them
keep their state in the Postgres database that already backs `events`.

```mermaid
flowchart LR
    S[APScheduler<br/>SQLAlchemy job store<br/>Asia/Kolkata] -->|enqueue| Q[(tasks table<br/>Postgres queue)]
    API[FastAPI /v1] -->|enqueue| Q
    Q -->|claim SKIP LOCKED| W[Worker]
    W --> G[LangGraph agent graph]
    G <-->|checkpoints| CP[(Postgres checkpointer)]
    G -->|upsert| CJ[(canonical_jobs)]
    G -->|auto-log| EV[(events)]
    G -->|interrupt| H{{Human approval<br/>UI / API}}
    H -->|Command resume| G
    G -->|drafts only| GM[Gmail drafts]
```

### 1. LangGraph agent graph with a Postgres checkpointer

The pipeline is a LangGraph `StateGraph`. Nodes are thin wrappers around the
existing domain functions, so the v1 engines are reused rather than rewritten:

`scan → upsert corpus → rank (new/changed only) → shortlist → generate docs → validate → approval gate → execute → record`

- Graph state is checkpointed after every node by `PostgresSaver`
  (`langgraph-checkpoint-postgres`). A crash or deploy resumes from the last
  completed node instead of starting over.
- One graph thread per (candidate, job) application candidate, so each
  shortlisted job can be paused, resumed or audited on its own.
- LLM calls go through the existing `backend/llm/provider.py`
  (Groq/Ollama). v2 adds no new model provider.

### 2. Postgres-backed task queue

A `tasks` table replaces the in-process thread and `_state` in
`api/routers/rankings.py`:

- Workers claim work with `SELECT … FOR UPDATE SKIP LOCKED`, take a lease with
  a heartbeat, and use bounded retries with backoff. Each task carries an
  idempotency key so a retried or duplicate enqueue never runs twice.
- Enqueue happens in the same transaction as the domain write that caused it.
- The `/v1/rankings` contract stays as it is (`POST` returns `202`, clients poll
  status), as the rankings module docstring promised.
- This fixes audit M7 (a single writer for ranking output) and L5 (no
  module-global state).

### 3. APScheduler with a SQLAlchemy job store, timezone `Asia/Kolkata`

- APScheduler 3.x with `SQLAlchemyJobStore` on the same database, so schedules
  survive restarts. The timezone is `Asia/Kolkata`; stored timestamps stay
  `TIMESTAMPTZ` (UTC).
- The scheduler **only enqueues tasks**. It never runs scans or ranking in its
  own process. Jobs use `coalesce=True`, `max_instances=1` and a
  misfire grace window, so a missed run fires once rather than as a burst.
- Exactly one scheduler process runs at a time. APScheduler 3.x does not
  support several schedulers sharing one job store.

### 4. The corpus lives in `canonical_jobs`

- `canonical_jobs` becomes the source of truth. Collectors **upsert** on
  `(company, normalized_title)`, which sets `first_seen_at` on insert and bumps
  `last_seen_at` on every re-sighting. That makes "new since last scan" a
  simple query and makes incremental scans possible.
- `jobs.json` and `sample_jobs.json` stay as a read-only export and fallback,
  so the database-less Streamlit Cloud demo keeps working.

### 5. Approval gate via `interrupt()`, and approvals log their own events

- The approval node calls LangGraph's `interrupt()` with the full review
  packet: job, score breakdown, tailored resume, cover letter, draft email and
  the validator report. The graph pauses durably in the checkpointer.
- The user approves, edits or declines from the UI/API. That resumes the graph
  with `Command(resume=…)`. Edited text goes back through the validator
  before execution.
- Every approval decision is written to `events` automatically, with the score
  snapshot (as `log_outcome()` does today). This replaces manual
  `log_outcome_cli` entry for anything the agent surfaced. The new event types
  (proposed: `approved`, `declined`, `drafted`) mean extending the `events`
  CHECK constraint in both `schema.sql` and `db.py`, plus an idempotent
  migration in `_run_migrations()`. That work belongs to Phase 1.

### Alternatives considered

| Alternative | Why it was rejected |
|---|---|
| Celery or RQ with Redis (suggested in DEPLOYMENT.md) | Adds a broker and another stateful service for one user's workload. A Postgres queue gives enqueue in the same transaction as the domain write, with no new infrastructure |
| Temporal | Real durability, but heavy to run and learn for this scope |
| Cron plus the existing CLIs | No run state, no resume, no human-in-the-loop pause |
| Hand-rolled state machine | Checkpointing and the human-approval pause would have to be rebuilt by hand; LangGraph already provides both |
| Keeping the corpus in JSON | No stable identity, no incremental scans, and append-only duplicates |

## Guardrails (non-negotiable)

These are enforced **structurally**, in what code exists and which edges the
graph has. Prompts are not relied on. Each one gets an automated test by
Phase 7.

1. **Never auto-submit applications.** No node, tool or worker task can submit
   an application. "Execute" means preparing the packet and the apply link.
   The human submits. An `applied` event is recorded only when the user
   confirms they submitted.
2. **Gmail: drafts only.** The Gmail client wrapper exposes `drafts.create`
   (and draft update/delete) and nothing else. Note that Google's
   `gmail.compose` scope *also allows sending*, so the scope alone does not
   prevent it. The wrapper does, and a test checks that no send method is
   reachable.
3. **ATS APIs only.** Jobs come only from the documented public job-board APIs
   (Greenhouse, Ashby, and Lever per verified slug), the same as v1. No
   scraping LinkedIn, Naukri or Indeed, and no headless-browser form filling.
4. **No CAPTCHA bypass.** If a source returns a CAPTCHA, a block or a rate
   limit, the agent backs off and reports it. It never solves, evades or
   outsources a CAPTCHA, and never rotates identity to get around a block.
5. **The truthfulness validator is mandatory on every generated document**:
   tailored resumes, cover letters, email drafts and follow-ups. In the graph,
   every path from `generate` to `approval` goes through `validate`, with the
   v1 fallback to the original bullets. Human edits are validated again.
   Flagged content is shown in the review packet, never silently dropped.

## Consequences

### Positive

- Runs and pending approvals survive restarts and deploys.
- One coordinated writer for ranking output, which closes audit M7 and L5.
- Incremental scans: only new or changed jobs need embedding and ranking.
- Outcome data builds up automatically from approval decisions. That unblocks
  learning-to-rank (about 50 outcomes needed) without relying on manual logging.
- A complete audit trail: every decision is an `events` row with the score
  snapshot and `FORMULA_VERSION` that were active when it was made.

### Negative / trade-offs

- **Postgres becomes required for the agent.** v1's core flows run on JSON
  files alone. The JSON fallback is kept for the hosted demo, but the
  always-on features need a database.
- **Two Postgres drivers.** `langgraph-checkpoint-postgres` uses psycopg 3,
  while the app uses `psycopg2-binary`. Both will be installed until the app
  moves to psycopg 3.
- **New dependencies that change quickly** (`langgraph`, its checkpointer,
  APScheduler, Google API client). Pin their versions. Checkpoint tables are
  owned by LangGraph (`setup()`) and are not part of `schema.sql`.
- **The queue polls.** That adds latency (seconds) and has a throughput
  ceiling compared with a dedicated broker. That is fine for one user. If
  multi-user load arrives, revisit with LISTEN/NOTIFY or a broker.
- **Only one scheduler process.** It needs an explicit deployment constraint
  (or an advisory lock) to prevent duplicate schedules.
- **Identity key weakness.** `(company, normalized_title)` merges the same title
  posted in several locations or with several requisition IDs. Phase 1 should
  look at adding the source's posting ID as a secondary key.
- **Gmail OAuth.** Refresh tokens are a new secret to store and rotate. A
  personal OAuth client in testing mode works for one user. A public
  multi-user release would need Google's OAuth verification for Gmail scopes.
- **Validator coverage has to grow.** Email drafts and follow-ups are new
  document types, and the validator has to handle them (per paragraph, as
  `cover_letter.validate_letter` does) before they can be generated.
- **More moving parts to run** (API, worker, scheduler, database) than v1's
  API, UI and database. Health checks and run logs are Phase 7 work.
