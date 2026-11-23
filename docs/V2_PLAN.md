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
| 3 | Scheduled incremental scan | APScheduler (SQLAlchemy job store, `Asia/Kolkata`) enqueues scans on schedule, a restart produces no duplicate or burst runs, and only jobs new or changed since the last scan are re-embedded and re-ranked. | ✅ Done |
| 4 | Agent graph dry-run | The LangGraph graph (scan → rank → shortlist → generate → validate → approval) runs end to end with the Postgres checkpointer and **zero side effects**, and a run killed mid-graph resumes from its last checkpoint. | ✅ Done |
| 5 | Approval + execution | The graph pauses at `interrupt()`; approve, edit or decline from the UI/API resumes it and auto-logs an `events` row; execution creates Gmail **drafts only**; and tests prove no code path can send email or submit an application. | ✅ Done |
| 6 | Memory + follow-ups | Declined companies and roles are not resurfaced, and N days after an `applied` event with no recorded reply a validated follow-up draft is queued for approval. | ✅ Done |
| 7 | Hardening + release | Guardrail test suite in CI, health checks and run logs for worker and scheduler, docs updated, CI green, and `v2.0.0` tagged from a reviewed merge to `main`. | ⏳ |

## Phase 3 — what shipped

- **Scheduler:** `python -m backend.scheduler` uses APScheduler 3.x with its job store in Postgres, timezone `Asia/Kolkata`, cron `SCAN_CRON` and misfire grace `SCAN_MISFIRE_GRACE_S`. It only enqueues `scan` tasks, keyed on the scheduled fire time. It elects a leader with a Postgres advisory lock, and a leader that loses the lock exits non-zero.
- **Scan task:** collect from each ATS board in isolation, upsert into `canonical_jobs`, then run an incremental rank. A failing board is reported in the result. Only an all-boards failure fails (and retries) the task.
- **Incremental ranking:** the new `job_scores` table caches the matcher's output per (job, profile hash, cache key), where the cache key is `FORMULA_VERSION` plus a fingerprint of the weights, gates, embedding model and matcher source. A rank scores only jobs without a current row and reproduces a full `rank_jobs` exactly (parity tests compare the JSON). Every rank execution holds a session-level advisory lock for the scoring and the file write.
- **API:** `POST /v1/scans` and `GET /v1/scans/latest`. `/v1/rankings/status` now reports a stalled task, whose lease expired, as "stalled".
- **Review fixes:** the scoring cache key became a fingerprint, so a weights, gate or matcher edit rescores without a manual `FORMULA_VERSION` bump. The two 404 boards were switched to their current names (`greenhouse/doordashusa`, `ashby/perplexity`). The scheduler's skip test no longer passes vacuously.

## Phase 4 — what shipped

The agent graph as a **dry run**. It drafts and records, and sends nothing: no email, no upload, no submission, and no network traffic except the LLM provider's. The scan stays a plain queue task that chains the agent. The graph is split in two at the queue ([ADR-001 addendum](adr/ADR-001-v2-always-on-architecture.md#addendum-phase-4-the-queue-and-langgraph-split)).

- **Agent run** (`agent_run` task, LangGraph thread `agent_run:<task_id>`): `refresh_ranking` (the incremental rank, under the rank lock) → `triage` (current profile and cache key, score ≥ `AGENT_MIN_SCORE`, seen within `AGENT_MAX_JOB_AGE_DAYS`, untouched, capped at `AGENT_MAX_DRAFTS_PER_RUN`) → `dispatch` (each `applications` row and its `application` task in one transaction). Queued by `POST /v1/agent/runs` and, with `AGENT_ENABLED=true`, by every scan (key `agent_run:scan:<scan task id>`).
- **Application** (`application` task, thread `application:<id>`): `load → extract_requirements → tailor_resume → validate_resume → cover_letter → validate_letter → render → persist`. There is one LLM call per LLM node, and the JD requirements are shared by both documents. The v1 validators run unchanged: any flag gives `needs_review`, otherwise `drafted`. Four artifacts with their sha256 go to `data/applications/<id>/`, and one `drafted` event is logged.
- **Durability:** the Postgres checkpointer (`langgraph-checkpoint-postgres`) with `durability="sync"`. A retried task resumes at the node its last attempt died in. The kill test SIGKILLs a worker inside `tailor_resume`: the second worker finishes with `extract_requirements` run once in total and the documents rendered once. A dead task marks its application `failed`.
- **Resume engine:** split into steps (`extract_jd_requirements`, `build_tailored_resume`, `draft_cover_letter`, `validate_letter`, `render_resume_docx`, `render_cover_letter`), with output paths as parameters. The CLIs' and UI's outputs are pinned byte for byte by golden tests. `LLM_PROVIDER=fake` gives CI a deterministic, offline LLM.
- **Data:** `applications` and `application_artifacts` (schema.sql, ORM, parity test). LangGraph owns its checkpoint tables and sets them up under the migration lock.
- **API:** `POST /v1/agent/runs`, `GET /v1/agent/runs/latest`, `GET /v1/applications`, `GET /v1/applications/{id}`, `GET /v1/applications/{id}/artifacts/{kind}`. There are no UI changes yet.
- **Image:** Node and the docx generators' `npm ci` are in the Docker image. CI builds it and renders both documents inside it.

## Phase 5 — what shipped

The approval gate and execution, with drafts only ([ADR-001 Phase 5 addendum](adr/ADR-001-v2-always-on-architecture.md#addendum-phase-5-the-approval-gate)).

- **Phase 4 review fixes:** triage keeps only jobs whose location, or a posting's, matches `AGENT_LOCATIONS` (whole words; remote only if it mentions India, unless `AGENT_ACCEPT_GLOBAL_REMOTE=true`), and the application uses the matching posting's `apply_link`. Scoring and ranking are untouched. Every package only LangGraph pulls in (langchain-core, langsmith, the checkpoint serializers, ...) is pinned exactly, in one block shared by both requirements files.
- **States:** `awaiting_approval` (with a `needs_review` flag and a documents `version`), `approved`, `ready_to_apply`, `email_drafted`, `declined`, `expired`. Phase 4's `drafted`/`needs_review` rows are migrated. New tables `approvals` (one decision per version) and `application_actions` (`packet`, `gmail_draft`); `events.source` (`user`, `expiry`, `agent`).
- **Graph:** `persist → await_approval` calls `interrupt()`; the task completes. An `application_decision` task resumes the same thread: approve → `execute` (approved event, `packet.json` → `ready_to_apply`) → `email_draft` (one Gmail draft if the JD has a contact address and Gmail is configured → `email_drafted`); decline / expire → `declined` / `expired` with a `declined` event; edit → re-validate, re-render, persist as version + 1, wait again. Approve, decline and expire make zero LLM calls. `approval_expiry` tasks (`AGENT_APPROVAL_TTL_DAYS`, 7) expire only a version still undecided.
- **Gmail:** `backend/integrations/gmail.py` has `create_draft()` and `find_draft_by_message_id()` and nothing that can send. OAuth installed-app flow for `gmail.compose`, token in `data/secrets/` (gitignored), on only with `GMAIL_CREDENTIALS_FILE`. The draft's action row is committed with its deterministic Message-ID before Gmail is called, so a retried task finds the draft instead of making a second one (kill-tested with a real SIGKILL).
- **API / UI:** `POST /v1/applications/{id}/decision` (202 / 409 / 422 / 503), `?status=awaiting_approval` sorted by score, the detail adds the packet, actions, decisions, documents and why-match. `API_TOKEN` guards every mutating endpoint. The Streamlit **Inbox** tab (database mode only) reviews and decides; the Tracker shows approved, declined and expired.

## Phase 6 — what shipped

Memory, follow-ups and sourcing ([ADR-001 Phase 6 addendum](adr/ADR-001-v2-always-on-architecture.md#addendum-phase-6-memory-follow-ups-and-one-letter-validator)).

- **Phase 5 review decisions:** `AGENT_MIN_SCORE` defaults to 20 (just under the p90 of the real corpus's eligible, gate-passing scores; 80 was above the maximum). `AGENT_EXCLUDE_TITLE_WORDS` (senior, sr, staff, principal, lead, manager, director, head, architect, vp, distinguished; whole words) is a triage-only seniority filter. FastAPI 0.165.0 and Starlette 1.10.0 are pinned in both requirements files and in CI.
- **One cover-letter validator** (`resume_engine/letter_validator.py`) for every letter: sentence-starting words and lower-case hyphenated words are prose, a known name (corpus company, taxonomy, curated list) that starts a sentence is still a claim. On the labelled fixture of the 3 real Phase 5 letters plus synthetic cases: precision 1.000, recall 1.000 (the bullet rules: 0.273 / 0.818). Resume-bullet validation is byte-identical; three `generate_for_job` goldens lost the false flag `Reached`.
- **Sourcing:** `python -m backend.job_scraper.probe_boards` keeps a public board only if it answers and has a posting in `AGENT_LOCATIONS`. Seven India boards were added (groww, observeai, inmobi, cred, meesho, atlan, sarvam). On the real corpus a scan took location-eligible jobs from 151 to 438 and eligible, gate-passing ones from 41 to 163.
- **Memory:** `agent_memories` (stated / learned, with evidence) learned deterministically from approve/decline decisions; triage-only (avoid excludes, prefer reorders by a bounded boost); `/v1/memories`; the Inbox's Memory section.
- **Follow-ups:** an `applied` event queues a `follow_up` task (same transaction, `AGENT_FOLLOW_UP_DAYS`); a later outcome cancels it; when due, `follow_up_due` and at most one follow-up Gmail draft, exactly once (kill-tested). The Inbox lists the follow-ups due.
- **Learning-to-rank export:** `python -m backend.ltr.export` and `GET /v1/ltr/export`, one labelled row per job with a decision or outcome. No training until 50 labelled outcomes exist.
