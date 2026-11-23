# API (FastAPI — implemented)

> **Status: implemented** for profile, analysis, rankings, scans, tracker, the agent, its memory and the learning-to-rank export (`backend/api/`). Document-generation endpoints (tailored resume, cover letter) remain CLI-only for now — they need Ollama and are the next candidates for async endpoints.

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
| GET | `/v1/applications` | `agent.store.list_applications` | Drafted applications, newest first (`status`, `limit`, `offset`, `sort=score\|newest`). `?status=awaiting_approval` is best score first |
| GET | `/v1/applications/{id}` | `agent.store.get_application` | Status, score, validator reports, task state, artifact list; why it matched, the documents' text, the packet, actions and decisions ([details](#approval-gate-phase-5)) |
| GET | `/v1/applications/{id}/artifacts/{kind}` | — | Download one drafted file (or the packet) |
| POST | `/v1/applications/{id}/decision` | `agent.decisions.submit_decision` → worker runs `agent.application_graph.run_decision_task` | Approve / decline / edit the version you reviewed (202). 409 if it isn't awaiting approval at that version or was already decided; 422 for a flagged edit without `acknowledge_flags`; 503 without a database ([details](#approval-gate-phase-5)) |
| GET | `/v1/tracker` | `database.tracker.get_tracked_jobs` | Tracked applications with derived status |
| POST | `/v1/tracker/events` | `database.events.log_outcome` | Log save/applied/interview/rejected/offer/dismiss. Phase 6: `applied` for a job the agent has an application for queues its follow-up; a later outcome clears a due one ([details](#follow-ups)) |
| GET | `/v1/memories` | `agent.memory.list_memories` | What the agent remembers: every memory with its provenance (`stated` / `learned`) and evidence (`kind`, `active`, `provenance` filters) ([details](#memory)) |
| POST | `/v1/memories` | `agent.memory.create_stated` | State a memory (201). Stating a learned one makes it yours; one you already stated is 409; a bad kind/value/weight 422 |
| PATCH | `/v1/memories/{id}` | `agent.memory.update` | Edit `value` / `weight` / `active`. 404 / 409 / 422 |
| DELETE | `/v1/memories/{id}` | `agent.memory.delete` | Remove a memory (204, 404) |
| GET | `/v1/ltr/export` | `ltr.export.export_rows` | The learning-to-rank dataset as CSV ([details](#learning-to-rank-export)); 503 without a database |

**Auth.** With `API_TOKEN` set, every mutating endpoint (`POST`/`PUT`/`PATCH`/`DELETE`) needs `Authorization: Bearer <API_TOKEN>`, else 401. Without it nothing is checked, so **never expose the API beyond localhost or a private network without `API_TOKEN`**. Reads stay unauthenticated even with a token, and they include personal documents (`GET /v1/applications/{id}/artifacts/...`), so keep the API off the public internet either way ([DEPLOYMENT.md](DEPLOYMENT.md#approval-gate-and-gmail-drafts)).

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
2. `triage` picks jobs from `job_scores` for the **current** profile and cache key. A job qualifies if it passed the gates, scored at least `AGENT_MIN_SCORE` (20), was seen within `AGENT_MAX_JOB_AGE_DAYS` (14), has no application yet, has no `applied`, `dismiss` or `declined` event, and is in a location `AGENT_LOCATIONS` / `AGENT_ACCEPT_GLOBAL_REMOTE` accept (its own or a posting's; a qualifying posting's `apply_link` becomes the application's), and has a title `AGENT_EXCLUDE_TITLE_WORDS` doesn't exclude (whole words: `senior`, `sr`, `staff`, `lead`, ...). Phase 6: it first re-learns your memories from your decisions, then applies the active ones: an `*_avoid` memory leaves matching jobs out, `*_prefer` memories add a bounded boost to the triage **order** of the jobs left (never to `match_score`, never past `AGENT_MIN_SCORE`; [memory](#memory)). The run's `result.excluded` counts every job over the score gate each rule left out (`location`, `title`, `memory`), and `result.memory` says how many memories applied and what learning changed. Best first, at most `AGENT_MAX_DRAFTS_PER_RUN` (5); each dispatched application records the memories that boosted it (`memory_effects`). See [CONFIGURATION.md](../CONFIGURATION.md#the-agent-phase-4-dry-run).
3. `dispatch` creates, for each job in **one transaction**, the `applications` row (`queued`) and its `application` task (idempotency key `application:<id>`). Re-running never duplicates one, because of `UNIQUE (user_id, job_id)`.

Each `application` task runs the application graph on thread `application:<id>`:

`load → extract_requirements → tailor_resume → validate_resume → cover_letter → validate_letter → render → persist`

- There is one LLM call per LLM node. The JD requirements are extracted once and used for both documents.
- The truthfulness validators are the resume engine's, unchanged. A flagged resume project keeps its original bullets, and a flagged letter paragraph is reported. Nothing is regenerated. Any flag sets **`needs_review: true`**.
- The files go to `data/applications/<id>/`: `resume.docx`, `cover_letter.docx`, `cover_letter.txt` and `jd_requirements.json`. `persist` records each one, with its sha256 and validator report, moves the application to **`awaiting_approval`** at version 1, queues its approval expiry and logs one `drafted` event with the score snapshot, all in one transaction. The graph then stops at the approval gate (below) and the task completes.
- A retried task resumes from its last checkpoint. A worker killed in `tailor_resume` costs one more `tailor_resume` call, and nothing before it runs again. An LLM outage fails the attempt, which is retried with backoff. Once the task is dead, the application is **`failed`** with the error.

```http
POST /v1/agent/runs          -> 202 {"state": "running", "task_id": 31, "trigger": "api", "detail": "Task #31 queued, waiting for a worker (attempt 0/3).", ...}
POST /v1/agent/runs          -> 409 {"detail": "An agent run is already in progress."}
GET  /v1/agent/runs/latest   -> 200 {"state": "done", "task_id": 31, "result": {"triaged": 2, "dispatched": [{"application_id": 7, "task_id": 32, "job_id": 1204, "match_score": 86.4, "title": "...", "company": "..."}, ...], "skipped_existing": 0, "refresh": {"scored": 0, "skipped_unchanged": 4676, ...}, "profile_hash": "...", "cache_key": "1:3f9c...", "settings": {...}, ...}}
GET  /v1/applications        -> 200 [{"id": 7, "job_id": 1204, "title": "...", "company": "...", "status": "awaiting_approval", "needs_review": true, "version": 1, "match_score": 86.4, "error": null, ...}]
GET  /v1/applications/7      -> 200 {..., "validator": {"resume": {"ok": false, "projects": [{"project": "...", "ok": false, "flagged": ["Kubernetes"]}, ...]}, "cover_letter": {"ok": true, "paragraphs": []}}, "artifacts": [{"kind": "resume_docx", "sha256": "...", "size_bytes": 10012, "download_url": "/v1/applications/7/artifacts/resume_docx", "report": {...}}, ...], "task": {"task_id": 32, "state": "done", "attempts": 1, ...}}
GET  /v1/applications/7/artifacts/cover_letter_txt -> 200 text/plain (X-Artifact-SHA256: ...)
```

| Status | Meaning |
|---|---|
| `queued` | Dispatched; its task is waiting for a worker |
| `drafting` | The graph is running (or waiting to retry after a failed attempt) |
| `awaiting_approval` | All four files written; waiting for your decision on `version`. `needs_review: true` means the validator flagged something: read `validator` first |
| `approved` | You approved; the worker is building the packet |
| `ready_to_apply` | The packet is ready (`packet`): apply yourself from `apply_link` |
| `email_drafted` | ...and one Gmail draft was created for the JD's contact address (never sent) |
| `declined` | You declined |
| `expired` | Nobody decided within `AGENT_APPROVAL_TTL_DAYS` |
| `failed` | The drafting task died; `error` has the last error |

Phase 4's `drafted` and `needs_review` are gone: the migration moved those rows to `awaiting_approval` (with the `needs_review` flag). Artifact kinds are `resume_docx`, `cover_letter_docx`, `cover_letter_txt`, `jd_requirements` and `packet`. An unknown kind, an application with no such file yet, or a file deleted from disk returns 404. `?status=` must be one of the statuses above (otherwise 422).

## Approval gate (Phase 5)

After `persist` the application graph calls LangGraph's `interrupt({application_id, version})`. The thread pauses in the checkpointer and the `application` task **completes** (`result.state = "awaiting_approval"`), so no worker waits for a person. A decision resumes the same thread with `Command(resume=decision)`. **Approve, decline and expire make no LLM call.** Nothing is ever sent or submitted: you apply yourself.

**`POST /v1/applications/{id}/decision`**

```json
{"action": "approve" | "decline" | "edit",
 "version": 1,                         // the version you reviewed
 "reason": "optional text",
 "edits": {"cover_letter_paragraphs": ["...", "..."],          // the whole body
           "resume_bullets": {"RAG Medical Copilot": ["...", "..."]}},
 "acknowledge_flags": false}
```

In **one transaction** it locks the application, checks it is `awaiting_approval` at `version` (else **409**), inserts the `approvals` row (one per version: a second decision on it is **409** too) and queues an `application_decision` task (key `decision:<approval_id>`). It answers **202** with `{approval_id, task_id, application_id, decision, version, source}`. The worker then:

- **approve:** logs an `approved` event (source `user`), writes `packet.json` (the documents with their sha256, the apply link, a job summary, why it matched, the gaps, the validator verdict) as the `packet` artifact → `ready_to_apply`. If the JD names a contact address (not no-reply / privacy / accommodation / support / ...) **and** Gmail is configured, it creates **one** Gmail draft with both `.docx` files attached → `email_drafted`.
- **decline:** `declined` plus a `declined` event with your reason.
- **edit:** your paragraphs and bullets are re-checked by the truthfulness validators (no LLM). If any edited text is flagged, the request is **422** with `{"detail": {"message": ..., "flags": [{"document": "cover_letter", "paragraph": 3, "flags": ["Google"]}, ...]}}` unless `acknowledge_flags` is true. An acknowledged edit is kept as you wrote it, flagged in the report, and sets `needs_review`. The worker re-renders the documents, persists them as **version + 1** (still `awaiting_approval`, with a new expiry) and the graph waits again.
- **expire:** an `approval_expiry` task (due `AGENT_APPROVAL_TTL_DAYS` after each version is drafted, key `expiry:<id>:<version>`) records `expire` (source `expiry`) only if that version is still awaiting with no decision. The worker then sets `expired` and logs a `declined` event with source `expiry`. Otherwise it does nothing.

```http
POST /v1/applications/7/decision {"action": "approve", "version": 1}   -> 202 {"approval_id": 3, "task_id": 41, "application_id": 7, "decision": "approve", "version": 1, "source": "user"}
POST /v1/applications/7/decision {"action": "approve", "version": 1}   -> 409 {"detail": "Version 1 of application 7 already has a decision."}
POST /v1/applications/8/decision {"action": "edit", "version": 2, ...} -> 409 {"detail": "Application 8 is at version 3, not 2; reload it."}
POST /v1/applications/9/decision {"action": "edit", "version": 1, "edits": {...}} -> 422 {"detail": {"message": "The edit has unverified claims; ...", "flags": [...]}}
GET  /v1/applications/7  -> 200 {..., "status": "ready_to_apply", "packet": {"job": {"apply_link": "...", "summary": "..."}, "documents": {...}, "why_match": {...}, "gaps": [...], ...}, "actions": [{"action": "packet", "status": "done", ...}], "approvals": [{"decision": "approve", "source": "user", "application_version": 1, ...}], "documents": {"cover_letter_paragraphs": [...], "resume_projects": [{"title": "...", "bullets": [...]}]}, "match": {"score_breakdown": {...}, "explanation": {"strong_matches": [...], "missing_skills": [...]}}}
```

The Streamlit **Inbox** tab (database mode only) shows the same queue and calls the same `submit_decision()`.

## Memory

What the agent remembers about your preferences (`backend/agent/memory.py`). **Triage only:** a memory never changes a score, the score gate, `job_scores` or any ranking output.

| `kind` | Effect in triage |
|---|---|
| `company_avoid` | jobs at this company are left out (letters and digits compared: `Sarvam AI` = `sarvamai`) |
| `title_avoid` | jobs whose title contains these word(s) are left out (whole words) |
| `company_prefer`, `title_prefer`, `location_prefer` | matching jobs get the memory's `weight` (score points) added to their triage ORDER key, capped in total at `AGENT_MEMORY_MAX_BOOST` (5) |
| `note` | none: a note for you |

**Provenance.** `stated` memories come only from you (`POST /v1/memories`, the Inbox) and learning never changes them. `learned` ones are derived from your own approve/decline decisions, deterministically and without an LLM, every time a decision is applied and before every triage:

- `company_avoid`: a company declined `AGENT_LEARN_COMPANY_DECLINES` (2) times and never approved;
- `title_avoid`: a title word declined `AGENT_LEARN_TITLE_DECLINES` (3) times and never approved (generic words such as `engineer` and location words are never learned);
- `*_prefer`: a company, title word or location (the `AGENT_LOCATIONS` entry it matched, never `India`, or `remote`) approved `AGENT_LEARN_PREFER_APPROVALS` (2) times and more often than declined; `weight` = approvals - declines.

Expiries and edits teach nothing. `evidence` lists every decision behind a learned memory (approval, application, job and event ids, title, company, decline reason). A learned memory whose evidence no longer meets its rule is removed. Learning never learns the opposite of a memory you stated and keep active. A **deactivated** memory has no effect and learning never touches it again; editing a learned memory's `value` or `weight` makes it `stated`. A deleted learned memory can be learned again from the same decisions: deactivate it to stop that. A stated memory's default `weight` is 2.

```http
POST  /v1/memories {"kind": "company_avoid", "value": "Acme"}          -> 201 {"id": 4, "kind": "company_avoid", "value": "Acme", "provenance": "stated", "evidence": {"stated_via": "api"}, "weight": 2.0, "active": true, ...}
POST  /v1/memories {"kind": "company_avoid", "value": "ACME"}          -> 409 {"detail": "You already have a company_avoid memory for 'Acme' (#4); edit it instead."}
GET   /v1/memories?provenance=learned                                   -> 200 [{"id": 7, "kind": "title_avoid", "value": "frontend", "provenance": "learned", "weight": 3.0, "evidence": {"rule": "declined 3 times (>= 3), never approved", "declined": [{"approval_id": 12, "application_id": 31, "event_id": 88, "reason": "not my field", ...}], "approved": [], "decline_reasons": ["not my field"]}, ...}]
PATCH /v1/memories/7 {"active": false}                                  -> 200 {..., "active": false, "provenance": "learned"}
DELETE /v1/memories/4                                                   -> 204
```

## Follow-ups

When you log `applied` (`POST /v1/tracker/events`, the Tracker, the CLI) for a job the agent has an application for, a `follow_up` task is queued **in the same transaction**, due `AGENT_FOLLOW_UP_DAYS` (7) later, key `follow_up:<application_id>` (once per application). When it runs:

- if an `interview`, `rejected`, `offer` or `dismiss` was logged for the job after `applied`, it does nothing;
- otherwise the application's `follow_up_due` becomes `true` (`GET /v1/applications`, the Inbox's **Follow-ups due**), and only if the job description names a contact address **and** Gmail is configured, it creates **one** follow-up Gmail draft (never sent): a fixed template (title, company, your name and contact lines; checked by the cover-letter validator), `In-Reply-To` the approval's draft if there was one. Exactly once: the `gmail_follow_up` action row is committed with the Message-ID `<aja-followup-<application_id>@ai-job-agent>` before Gmail is called, and a retry looks the draft up first (`tests/test_follow_up_kill.py` kills a worker in that window).

Logging a later outcome clears `follow_up_due`.

## Cover-letter truthfulness (Phase 6)

Every cover letter (the UI, the CLI, the agent's drafts and edits, the follow-up template) is checked by `backend/resume_engine/letter_validator.py`; resume bullets keep `validate_tailored.validate_bullet`, unchanged. Same numbers check and lookup as the bullet validator, plus three letter rules: a capitalized word that starts a sentence is not a claim on its capital alone; a hyphenated token is checked part by part (`cutting-edge` is prose, `Spark-based` is checked); and a **known name** that starts a sentence (a company in the job corpus, a skill-taxonomy name, or a short curated list of big companies and tools) is flagged unless your profile has it or it is the target company. Multi-word names are one term (`Hugging Face`). On the labelled fixture (`tests/fixtures/letter_validator/`) that is precision 1.000 and recall 1.000, against 0.273 / 0.818 with the bullet rules.

## Learning-to-rank export

`GET /v1/ltr/export` (and `python -m backend.ltr.export --out data/ltr/outcomes.csv`) returns one CSV row per (user, job) with a decision or an outcome. **Export only:** nothing is trained yet; training starts once there are at least **50** labelled rows (`X-Training-Minimum`; `X-Labelled-Outcomes` is the row count). An empty database returns the header only.

| Column | Meaning |
|---|---|
| `user_id`, `job_id`, `application_id` | ids (`application_id` empty for a job the agent never drafted for) |
| `company`, `title`, `location` | the job (the matched posting's location) |
| `label` | the furthest stage reached: **0** declined / expired / dismissed, **1** approved, **2** applied (or rejected, which implies applying), **3** interview or offer |
| `label_name` | the event that set the label |
| `match_score`, `skill_overlap`, `semantic`, `role`, `seniority` | score features at decision time: the first labelled event's snapshot, else the application's `job_scores` row |
| `cache_key` | the scoring regime of the application's score |
| `memory_boost`, `memory_effects` | triage's memory effects (the boost, and the memories as JSON) |
| `events` | the labelled events in order, e.g. `approved>applied>interview` |
| `first_event_at`, `label_at`, `last_event_at` | UTC ISO 8601 timestamps |

Impressions, clicks, saves and `drafted` are not decisions: a job with only those has no row. It is a read, so it needs no `API_TOKEN`; it holds your decisions, so keep the API private.

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
- Auth: single-user today (`users` table already exists). `API_TOKEN` guards the mutating endpoints (above); JWT is planned with multi-user.
- Versioning: `/v1` prefix from day one; score-formula changes bump a `formula_version` field in responses (the events table already snapshots scores for exactly this reason).
