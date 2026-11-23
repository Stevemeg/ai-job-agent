# API (FastAPI — implemented)

> **Status: implemented** for profile, analysis, rankings, scans, and tracker (`backend/api/`). Document-generation endpoints (tailored resume, cover letter) remain CLI-only for now — they need Ollama and are the next candidates for async endpoints.

Run from project root:

```bash
uvicorn backend.api.app:app --reload
# interactive docs: http://localhost:8000/docs
```

Design rules: routers contain no business logic (every endpoint is a thin adapter over a tested engine function); heavy imports (torch, PyMuPDF, SQLAlchemy) are deferred so the API boots instantly and non-dependent routes work when Postgres/Ollama are down; everything is versioned under `/v1`; `/healthz` exposes `formula_version` so clients can detect scoring-regime changes.

## Endpoints (implemented)

| Method | Path | Backing function | Description |
|---|---|---|---|
| GET | `/healthz` | — | Liveness + `formula_version` |
| POST | `/v1/resumes` | `resume_parser.api.parse_resume_pdf` | Upload PDF → parsed profile + parse confidence |
| GET | `/v1/profile` | — | Current profile |
| PUT | `/v1/profile` | `resume_parser.api.save_profile` | Save user-corrected profile (Review & Edit) |
| GET | `/v1/profile/health` | `analysis.resume_health.compute_health` | Health score + breakdown + findings |
| GET | `/v1/profile/gaps` | `analysis.skill_gap.compute_skill_gaps` | Role-weighted skill gaps |
| GET | `/v1/profile/careers` | `analysis.career_recommender.recommend_careers` | Role fits + evidence + missing |
| GET | `/v1/profile/suggestions` | `analysis.suggestions.generate_suggestions` | Evidence-backed suggestions |
| POST | `/v1/rankings` | `services.ranking.request_ranking` → worker runs `services.ranking.execute_rank` | Queue a ranking run (202). 409 while one is active; 503 without a database ([details](#rankings-on-the-task-queue)) |
| GET | `/v1/rankings/status` | `services.ranking.status_from_task` | Poll ranking state |
| GET | `/v1/rankings` | — | Ranked results (limit/offset/min_score) |
| POST | `/v1/scans` | `services.scan.request_scan` → worker runs `services.scan.run_scan_task` | Queue a scan now: collect, upsert, re-rank new/changed jobs (202). 409 while one is active; 503 without a database ([details](#scans)) |
| GET | `/v1/scans/latest` | `services.scan.scan_status` | The latest scan's state and, once done, its stats |
| POST | `/v1/agent/runs` | `agent.run_graph.request_agent_run` → worker runs the agent run graph | Queue an agent run: refresh the ranking, triage, dispatch drafts (202). 409 while one is active or with no profile; 503 without a database ([details](#agent-phase-4-dry-run)) |
| GET | `/v1/agent/runs/latest` | `agent.run_graph.agent_run_status` | The latest agent run's state and, once done, what it triaged and dispatched |
| GET | `/v1/applications` | `agent.store.list_applications` | Drafted applications, newest first (`status`, `limit`, `offset`) |
| GET | `/v1/applications/{id}` | `agent.store.get_application` | Status, score, validator reports, task state, artifact list |
| GET | `/v1/applications/{id}/artifacts/{kind}` | — | Download one drafted file |
| GET | `/v1/tracker` | `database.tracker.get_tracked_jobs` | Tracked applications with derived status |
| POST | `/v1/tracker/events` | `database.events.log_outcome` | Log save/applied/interview/rejected/offer/dismiss |

## Rankings on the task queue

Ranking takes minutes, so it runs on the durable worker ([ADR-001 §2](adr/ADR-001-v2-always-on-architecture.md)), not in the API process. The contract from v1 is unchanged: `POST` returns `202`, clients poll `/status`, and `GET /v1/rankings` serves the same payload. What changed is where the work runs.

`POST /v1/rankings` enqueues a `rank` task in the Postgres `tasks` table with `concurrency_key = "rank:default"`. The worker (`python -m backend.worker`, see [DEPLOYMENT.md](DEPLOYMENT.md#task-worker)) claims it, ranks the profile against the **database** corpus, and writes `data/ranked_jobs.json`. Every write of that file goes through `services.ranking` (the worker; with no database, the Streamlit demo and the CLIs).

Since Phase 3 a rank run is **incremental**. Each job's matcher output is cached in `job_scores` per (job, profile hash, cache key), and only jobs with no cached row for their current content are scored. The cache key is `<FORMULA_VERSION>:<fingerprint[:12]>`, where the fingerprint hashes `WEIGHTS`, `HARD_FILTERS`, `EMBED_MODEL` and the matching engine's source code, so a new profile, a formula bump, or any edit to the weights, gates or matcher rescores everything. Responses still report the bare `formula_version`. The assembled list is exactly what a full `rank_jobs` would return, and `GET /v1/rankings` is unchanged. Every rank execution, from this endpoint or from a scan, holds a Postgres advisory lock for the scoring and the file write, so two never interleave. The task `result` keeps `ranked_count`, `corpus_version` and `formula_version`, and adds `scored`, `skipped_unchanged` and `profile_hash`.

| Response | When |
|---|---|
| `202` + `RankingStatus` (`state: running`, detail names the task id) | Task queued |
| `404` | No profile yet (`POST /v1/resumes` first) |
| `409` "A ranking run is already in progress." | A `rank` task is already queued or running (enforced by a partial unique index, so it holds across API processes) |
| `409` "No jobs in the database corpus…" | The database holds no collected jobs. Import with `python -m backend.database.corpus import [path]` or run the collector |
| `503` | **No database configured** (neither `DATABASE_URL` nor `DB_HOST`). There is no queue without one, so the API won't rank in-process. The DB-less Streamlit demo still ranks inline. Also returned when a configured database is unreachable |

`GET /v1/rankings/status` maps the latest `rank` task onto the existing `RankingStatus` schema:

| Task | `state` | `detail` / `ranked_count` |
|---|---|---|
| none yet, ranked file exists | `done` | "loaded from previous run" (as in v1) |
| none yet, no ranked file | `idle` | — |
| `queued` (not started) | `running` | "Task #N queued, waiting for a worker (attempt 0/3)." |
| `running` | `running` | "Task #N running (attempt n/3)." |
| `running`, lease expired (`lease_expires_at < now()`, database clock) | `running` | "Task #N stalled — waiting for a worker to reclaim (attempt n/3, lease expired)." Its worker died or hung; the next worker to poll takes it over (or marks it dead if that was its last attempt) |
| `queued` after a failed attempt | `running` | "Task #N retrying: attempt n/3 failed (…error…)." Retries back off exponentially with jitter |
| `succeeded` | `done` | `ranked_count` from the task result |
| `dead` (attempts exhausted) | `failed` | the last error (`Type: message`) |

Without a database, `/status` never errors: it reports `done` ("loaded from previous run") or `idle` from the ranked file alone. With a configured but unreachable database it returns `503`.

## Scans

A scan refreshes the corpus and the rankings in one task. It collects from the public ATS boards (Greenhouse, Lever, Ashby), upserts into `canonical_jobs`, then runs an incremental rank. The worker runs it as a `scan` task. The scheduler (`python -m backend.scheduler`, see [DEPLOYMENT.md](DEPLOYMENT.md#scan-scheduler)) queues one every `SCAN_CRON` (default 09:00 IST). `POST /v1/scans` queues one now.

| Response | When |
|---|---|
| `202` + `ScanStatus` (`state: running`, `trigger: "api"`) | Scan queued |
| `409` "A scan is already in progress." | A scan, scheduled or manual, is already queued or running (`concurrency_key = "scan:default"`) |
| `503` | No database configured, or the configured one doesn't answer (probed with `DB_CONNECT_TIMEOUT`) |

No profile is needed. Without one the scan still refreshes the corpus and skips ranking (`result.rank_skipped`, `profile_hash: null`).

`GET /v1/scans/latest` returns the latest scan task as `ScanStatus`. It is `503` without a database, and `state: idle` before the first scan.

| Field | Meaning |
|---|---|
| `state`, `detail` | `idle`, `running`, `done` or `failed`, with the same wording as `/v1/rankings/status` (queued, retrying, stalled, running, or the last error) |
| `task_id`, `attempts`, `max_attempts` | The `tasks` row (3 attempts) |
| `trigger`, `scheduled_for` | `"schedule"` (with the slot's fire time, UTC) or `"api"` |
| `created_at`, `started_at`, `finished_at` | Timestamps (UTC) |
| `result` | Set when `done`, otherwise `null` |

`result` fields:

| Field | Meaning |
|---|---|
| `inserted`, `updated`, `changed` | From the upsert: new jobs, re-sighted jobs, and re-sighted jobs whose content changed |
| `scored`, `skipped_unchanged` | Jobs scored by this scan's rank, and jobs whose cached score was still current. An unchanged re-scan scores 0 |
| `sources_ok`, `sources_failed` | Boards fetched, and `[{source, slug, error}]` for the ones that failed after the collector's retries. A failing board doesn't fail the scan. Only when **every** board fails does the attempt fail and retry with backoff |
| `duration_s` | Wall time of the whole scan |
| `corpus_version`, `profile_hash`, `formula_version`, `ranked_count` | What was ranked, for whom, and how many jobs passed the gates |

```http
POST /v1/scans               -> 202 {"state": "running", "task_id": 12, "trigger": "api", "detail": "Task #12 queued, waiting for a worker (attempt 0/3).", ...}
GET  /v1/scans/latest        -> 200 {"state": "done", "task_id": 12, "result": {"inserted": 0, "updated": 4676, "changed": 0, "scored": 0, "skipped_unchanged": 4676, "sources_ok": 23, "sources_failed": [{"source": "greenhouse", "slug": "doordash", "error": "HTTP 404 (board not found -- renamed or removed token?)"}, ...], "duration_s": 65.62, "ranked_count": 1609, ...}}
```

(The `result` above is from a real re-scan of the live boards right after a first scan: nothing had changed, so nothing was scored. The first scan inserted and scored all 4,676 jobs in about 9 minutes on a laptop CPU. The two boards that failed then, `greenhouse/doordash` and `ashby/perplexity-ai`, have since been switched to their current names, `doordashusa` and `perplexity`.)

If `AGENT_ENABLED=true` and a profile exists, the scan's last step queues an agent run, keyed `agent_run:scan:<scan task id>` so a retried scan never queues a second one. The task row's `result` then also has `agent_run: {"task_id": n}`. That key is not part of `ScanResult`, and the `/v1/scans` responses are unchanged.

## Agent (Phase 4, dry run)

The agent drafts a tailored resume and cover letter for the best new jobs. **Nothing leaves the system.** There is no email, no upload and no submission. The only network traffic is the LLM provider's. Two LangGraph graphs run as worker tasks, each checkpointed in Postgres ([ADR-001 addendum](adr/ADR-001-v2-always-on-architecture.md#addendum-phase-4-the-queue-and-langgraph-split)).

**`POST /v1/agent/runs`** queues an `agent_run` task. Its graph, on thread `agent_run:<task_id>`:

1. `refresh_ranking` runs the same incremental rank as `rank` and `scan` tasks, under the rank lock. When nothing changed it scores nothing.
2. `triage` picks jobs from `job_scores` for the **current** profile and cache key. A job qualifies if it passed the gates, scored at least `AGENT_MIN_SCORE` (80), was seen within `AGENT_MAX_JOB_AGE_DAYS` (14), has no application yet, and has no `applied`, `dismiss` or `declined` event. Best first, at most `AGENT_MAX_DRAFTS_PER_RUN` (5). See [CONFIGURATION.md](../CONFIGURATION.md#the-agent-phase-4-dry-run).
3. `dispatch` creates, for each job in **one transaction**, the `applications` row (`queued`) and its `application` task (idempotency key `application:<id>`). Re-running never duplicates one, because of `UNIQUE (user_id, job_id)`.

Each `application` task runs the application graph on thread `application:<id>`:

`load → extract_requirements → tailor_resume → validate_resume → cover_letter → validate_letter → render → persist`

- There is one LLM call per LLM node. The JD requirements are extracted once and used for both documents.
- The truthfulness validators are the resume engine's, unchanged. A flagged resume project keeps its original bullets, and a flagged letter paragraph is reported. Nothing is regenerated. Any flag makes the status **`needs_review`**; otherwise it is **`drafted`**.
- The files go to `data/applications/<id>/`: `resume.docx`, `cover_letter.docx`, `cover_letter.txt` and `jd_requirements.json`. `persist` records each one, with its sha256 and validator report, and logs one `drafted` event with the score snapshot, all in one transaction.
- A retried task resumes from its last checkpoint. A worker killed in `tailor_resume` costs one more `tailor_resume` call, and nothing before it runs again. An LLM outage fails the attempt, which is retried with backoff. Once the task is dead, the application is **`failed`** with the error.

```http
POST /v1/agent/runs          -> 202 {"state": "running", "task_id": 31, "trigger": "api", "detail": "Task #31 queued, waiting for a worker (attempt 0/3).", ...}
POST /v1/agent/runs          -> 409 {"detail": "An agent run is already in progress."}
GET  /v1/agent/runs/latest   -> 200 {"state": "done", "task_id": 31, "result": {"triaged": 2, "dispatched": [{"application_id": 7, "task_id": 32, "job_id": 1204, "match_score": 86.4, "title": "...", "company": "..."}, ...], "skipped_existing": 0, "refresh": {"scored": 0, "skipped_unchanged": 4676, ...}, "profile_hash": "...", "cache_key": "1:3f9c...", "settings": {...}, ...}}
GET  /v1/applications        -> 200 [{"id": 7, "job_id": 1204, "title": "...", "company": "...", "status": "needs_review", "match_score": 86.4, "error": null, ...}]
GET  /v1/applications/7      -> 200 {..., "validator": {"resume": {"ok": false, "projects": [{"project": "...", "ok": false, "flagged": ["Kubernetes"]}, ...]}, "cover_letter": {"ok": true, "paragraphs": []}}, "artifacts": [{"kind": "resume_docx", "sha256": "...", "size_bytes": 10012, "download_url": "/v1/applications/7/artifacts/resume_docx", "report": {...}}, ...], "task": {"task_id": 32, "state": "done", "attempts": 1, ...}}
GET  /v1/applications/7/artifacts/cover_letter_txt -> 200 text/plain (X-Artifact-SHA256: ...)
```

| Status | Meaning |
|---|---|
| `queued` | Dispatched; its task is waiting for a worker |
| `drafting` | The graph is running (or waiting to retry after a failed attempt) |
| `drafted` | All four files written; the validator flagged nothing |
| `needs_review` | All four files written; the validator flagged something. Read `validator` before using them |
| `failed` | The task died; `error` has the last error |

Artifact kinds are `resume_docx`, `cover_letter_docx`, `cover_letter_txt` and `jd_requirements`. An unknown kind, an application with no such file yet, or a file deleted from disk returns 404. `?status=` must be one of the statuses above (otherwise 422).

## Endpoints (planned)

| Method | Path | Backing function | Notes |
|---|---|---|---|
| POST | `/v1/documents/resume` | `resume_engine.resume_tailor` | Async; needs Ollama |
| POST | `/v1/documents/cover-letter` | `resume_engine.cover_letter` | Async; needs Ollama |
| — | Auth (register/login/JWT) | — | With multi-user; `users` table exists |

## Example

```http
GET /v1/profile/health

200 OK
{
  "score": 91.0,
  "grade": "A",
  "breakdown": {
    "Quantified Impact": {
      "score": 6.0, "max": 15,
      "findings": ["Only 4/10 bullets contain numbers. ..."]
    }
  }
}
```

## Design Notes

- Ranking and document generation are **async jobs** (returns 202) because they take minutes and must not block a request thread. Ranking runs on the Postgres task queue and the worker (above); the 202 body's `detail` names the task id. Document generation is next.
- Auth: single-user today (`users` table already exists); JWT planned with multi-user.
- Versioning: `/v1` prefix from day one; score-formula changes bump a `formula_version` field in responses (the events table already snapshots scores for exactly this reason).
