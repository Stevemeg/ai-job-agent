# Deployment

## LLM Provider: Ollama locally, Groq in production

The engines call `backend.llm.generate(prompt)`; the provider is selected automatically:

```
GROQ_API_KEY present  →  Groq  (llama-3.3-70b-versatile, hosted)
otherwise             →  Ollama (mistral, localhost — dev only)
```

Deployed apps cannot reach a developer's local Ollama, so production uses Groq. The prompts, hallucination validator, validation reports, and .docx/.txt exports are identical for both providers. Configuration details: [CONFIGURATION.md](../CONFIGURATION.md).

**Privacy note:** with Groq, resume content IS sent to a hosted API — the local-first "never leaves the device" property applies only to the Ollama path. Disclose this in any public deployment.

## Streamlit Cloud

1. Push the repo to GitHub (`.gitignore` already excludes secrets, data, venv)
2. share.streamlit.io → New app → repo → main file `backend/ui/app.py`
3. Settings → Secrets → add `GROQ_API_KEY = "gsk_..."`
4. Deploy

Cloud caveats (honest list):
- **Ranking:** requirements.txt includes torch/sentence-transformers — the build is heavy but works on Community Cloud; first ranking run is slow. Prefer committing a pre-ranked `ranked_jobs.json` or using the sample corpus for demos. With no database configured this is **demo mode**: the "Rank jobs now" button ranks inside the Streamlit process, the only in-process ranking path left. If you add `DB_*` secrets, ranking moves to the task queue and needs a running worker ([Task worker](#task-worker)), which Community Cloud can't host.
- **.docx generation needs Node.js**: add a `packages.txt` containing `nodejs` and `npm`, and the docx npm package must be installable (`backend/resume_engine/node_modules` is gitignored). Until wired, cover letters fall back to .txt gracefully; resume tailoring reports the error. A python-docx fallback is the planned proper fix.
- **Tracker needs Postgres**: point `DB_*` env at a hosted Postgres (e.g. Neon/Supabase free tier) via secrets, or the Tracker tab shows its graceful "unreachable" panel.

## Current Model: Local-First

The system runs entirely on the user's machine by design — resume data is sensitive, and local Ollama inference means **no resume content ever leaves the device**.

| Component | Runtime | Required for |
|---|---|---|
| Python 3.11 + venv | Always | Everything |
| Streamlit | `streamlit run backend/ui/app.py` | UI |
| Ollama + Mistral | `ollama serve` | Tailoring, cover letters |
| PostgreSQL 16 | Docker Compose | Outcome logging; the task queue + worker (queued ranking and scans); the scheduler's job store |
| Node.js ≥ 18 | Invoked by Python | `.docx` generation |

### Postgres

```bash
cd backend/database
cp .env.example .env        # set credentials
docker compose up -d
```

Docker Desktop does not auto-start on Windows by default — enable "Start Docker Desktop when you log in", or bring it up manually per session. Everything except `--log` and outcome logging degrades gracefully without it.

## One-Command Full Stack (Docker Compose)

```bash
docker compose up --build
# UI:  http://localhost:8501
# API: http://localhost:8000/docs
# DB:  localhost:5432

# First run only: seed the database corpus the worker ranks against
docker compose exec api python -m backend.database.corpus import data/sample_jobs.json
#   (or data/jobs.json after running the collector)
```

One shared image serves the API, UI, **worker** and **scheduler** (same engines, different command). Design notes:

- `./data` and `./uploads` are **bind-mounted** — profiles, corpora, and rankings persist on the host and stay shared with host-side CLI runs
- The embedding model caches in a named `models` volume (`HF_HOME=/models`) — the ~90 MB download happens once, not per container
- The API waits for Postgres via healthcheck (`condition: service_healthy`); its own `/healthz` backs the container healthcheck
- `requirements-docker.txt` is a curated runtime set, not the dev-machine freeze — smaller, Linux-clean image
- **Ollama stays host-side** (resume tailoring / cover letters are CLI workflows); `backend/database/docker-compose.yml` still works standalone for DB-only development
- Credentials override via `.env` at the repo root (`DB_USER` / `DB_PASSWORD` / `DB_NAME`)
- The `worker` service runs `python -m backend.worker`. It waits for a healthy database, shares `./data` (profile in, `ranked_jobs.json` out) and the `models` cache with the API. `stop_grace_period: 45s` is deliberately longer than the worker's `--grace 30`, so a `docker compose stop` lets the current task finish instead of being SIGKILLed
- The `scheduler` service runs `python -m backend.scheduler` ([Scan scheduler](#scan-scheduler)). It waits for a healthy database and needs no volumes. `SCAN_CRON` / `SCAN_MISFIRE_GRACE_S` pass through from `.env`. Run **one** copy. A second copy is harmless (it waits in standby), but it adds nothing

## Task worker

`python -m backend.worker` is the consumer of the Postgres task queue (`tasks` table, [ADR-001 §2](adr/ADR-001-v2-always-on-architecture.md)). It runs `rank` tasks, queued by `POST /v1/rankings` and the Streamlit re-rank buttons. It runs `scan` tasks, queued by the scheduler and by `POST /v1/scans`. And it runs the agent's `agent_run` and `application` tasks ([below](#agent-dry-run)).

```bash
DB_HOST=localhost python -m backend.worker            # all registered kinds
python -m backend.worker --kinds rank --lease 60 --grace 30 --poll-max 10
```

- **Needs a database.** The worker refuses to start (exit code `2`, with a message on stderr) when neither `DATABASE_URL` nor `DB_HOST` is set, or when the database doesn't answer within `DB_CONNECT_TIMEOUT` (default 3s). On start it runs the idempotent schema migrations under a Postgres advisory lock, so API, UI and worker can boot together safely.
- **Claims** use `SELECT … FOR UPDATE SKIP LOCKED`, so any number of workers can run side by side; each task runs on one of them. Its `worker_id` is `host:pid:short-uuid`.
- **Leases.** A claimed task is leased for `--lease` seconds and heartbeated every lease/3. If the worker dies (crash, OOM, `kill -9`), the lease expires and another worker reclaims the task. Each claim counts as an attempt. A worker whose lease was reclaimed can no longer complete or fail the task: those writes are fenced on the lease holder.
- **Retries.** A failed attempt is requeued with capped exponential backoff and jitter (5s, 10s, … up to 5 min). After `max_attempts` (3 for ranking) the task is `dead`, and `/v1/rankings/status` reports `failed` with the last error.
- **Shutdown.** SIGTERM or SIGINT stops claiming and lets the current task finish within `--grace` seconds, then the worker exits `0`. If the task is still running when the grace period ends, the worker exits `3` and the task is recovered through lease expiry. A second signal exits immediately.
- **Corpus.** `rank` tasks read the corpus from Postgres only (strict mode, no JSON fallback), so seed it first with `python -m backend.database.corpus import [path]`, or let a scan collect it.
- **One rank at a time.** Every rank execution takes the rank lock: a session-level Postgres advisory lock on a dedicated connection, held for the scoring and the `ranked_jobs.json` write. That covers `rank` tasks, the rank step of a `scan`, and the CLIs when a database is configured. The lock is released when its holder's connection closes, so a killed worker never leaves it behind. A waiter gives up after 30 minutes, and its attempt is retried.
- **Incremental.** Ranking only scores jobs without a current `job_scores` row for (job, profile hash, cache key). The cache key is `FORMULA_VERSION` plus a fingerprint of the weights, hard filters, embedding model and matcher source, so the first rank after a resume upload, a `FORMULA_VERSION` bump, or a deploy that changes any of those scores the whole corpus (minutes with the embedding model). Later ranks only score the delta. Rows for old profiles stay in `job_scores` (a few thousand per profile); nothing prunes them yet.
- No database at all (the Streamlit Cloud demo) means no queue and no worker. The API's `POST /v1/rankings` returns 503 and the Streamlit app ranks inline.

## Scan scheduler

`python -m backend.scheduler` puts the corpus refresh on a clock ([ADR-001 §3](adr/ADR-001-v2-always-on-architecture.md)). It is APScheduler 3.x with its job store in Postgres (table `apscheduler_jobs`, created and owned by APScheduler, not `schema.sql`), running in timezone **Asia/Kolkata**. It has one job, `scan:scheduled`, on `SCAN_CRON` (default `0 9 * * *`, i.e. 09:00 IST / 03:30 UTC). See [CONFIGURATION.md](../CONFIGURATION.md#scheduled-scans).

```bash
DB_HOST=localhost python -m backend.scheduler
```

- **It only enqueues.** Each due run inserts one `scan` task and returns. The worker collects, upserts and ranks. The scheduler never imports the ML stack.
- **One task per slot.** The task's idempotency key is `scan:scheduled:<scheduled fire time, UTC>`. It comes from the *scheduled* time, not the clock at execution, so re-firing a slot after a restart or crash is a no-op. If a scan (say, a manual `POST /v1/scans`) is still queued or running at fire time, the scheduled one is logged and skipped.
- **Missed runs** (`coalesce`, `max_instances=1`, `misfire_grace_time = SCAN_MISFIRE_GRACE_S`). If the scheduler was down at fire time and comes back within the grace window, the slot runs once. If it comes back later, the slot is skipped. Several missed slots produce one run. A restart keeps the stored next run time unless `SCAN_CRON` changed.
- **Leader election.** APScheduler 3.x can't share a job store between processes, so the scheduler takes `pg_try_advisory_lock` on a dedicated connection with TCP keepalives. A second copy waits in **standby**, retrying every 15 seconds, and starts nothing that could fire. If the leader's lock connection dies, Postgres releases the lock. The leader then fires nothing more (it re-checks the lock before every round of due jobs and leaves the stored next run time for whoever leads next) and exits with code `3`. `restart: unless-stopped` brings it back.
- **Exit codes:** `0` stopped (SIGTERM/SIGINT), `2` no reachable database, `3` lost the leader lock, `4` bad `SCAN_CRON` / `SCAN_MISFIRE_GRACE_S`.
- Without a database there is no scheduler. Refresh the JSON corpus by hand with `python -m backend.job_scraper.ats_collector`.

## Agent (dry run)

Phase 4 adds two task kinds that the same worker runs: `agent_run`, queued by `POST /v1/agent/runs` and, with `AGENT_ENABLED=true`, by every scan; and `application`, queued by an agent run, one per triaged job. Both are LangGraph graphs checkpointed in Postgres. See [API.md](API.md#agent-phase-4-dry-run).

- **No outbound actions.** The agent drafts files and records them. It sends no email, uploads nothing and submits nothing. The only network traffic is the LLM provider's.
- **LLM.** Configure the worker's provider as for the UI. In Docker that means `GROQ_API_KEY` in `.env`: compose passes it to the worker, and Ollama on the host isn't reachable from containers. `LLM_PROVIDER=fake` drafts offline with deterministic text (for tests and smoke runs).
- **Node.** The worker renders `.docx` files with `backend/resume_engine/*.js`. The image installs Node and runs `npm ci` for them. On a bare host, install Node and run `npm ci` in `backend/resume_engine`.
- **Files.** Drafts are written to `data/applications/<application_id>/` and recorded with their sha256 in `application_artifacts`. In compose, `./data` is bind-mounted into the worker and the API, so the API serves the worker's files.
- **Checkpoints.** LangGraph's tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) are created by the worker on first use, under the same advisory lock as the app's migrations, in the app's database and schema. They are not in `schema.sql`. Each graph step is committed before the next starts (`durability="sync"`). So a retried task resumes at the node its last attempt died in, and nodes it finished are not run again. Nothing prunes old threads yet.
- **Failure.** An LLM outage or a failing node fails the attempt, which is retried with backoff like any task. After the last attempt the application is marked `failed` with the error. That also happens when a worker dies on the last attempt and its lease expires.
- **Turning it on.** `AGENT_ENABLED=true` (and optionally `AGENT_MIN_SCORE`, `AGENT_MAX_JOB_AGE_DAYS`, `AGENT_MAX_DRAFTS_PER_RUN`) in the worker's environment makes every scan chain an agent run. Manual runs work either way. See [CONFIGURATION.md](../CONFIGURATION.md#the-agent-phase-4-dry-run).

## Single-Host Server Deployment (small-team demo)

```
[Caddy/nginx TLS] → [docker compose up]   (UI 8501, API 8000, Postgres internal)
                     [ollama serve]        (host, GPU strongly recommended)
```

Add before exposing publicly: an auth proxy in front of Streamlit (it has no auth), per-user profile isolation (currently single-profile), and upload size limits.

## Production SaaS Path (planned)

1. **API extraction** — FastAPI over `analysis/` + `matching_engine/` ([API.md](API.md)); Streamlit (later React) becomes a client
2. **Workers** — ranking runs on the Postgres task queue and `python -m backend.worker` (done in v2 Phase 2; a Postgres queue was chosen over Celery/RQ, see ADR-001). LLM document generation moves onto the same queue next
3. **Storage** — profiles → Postgres; job corpus → Postgres + precomputed embedding index (encode corpus once per refresh, not per user)
4. **LLM serving** — pool of Ollama instances or a hosted-model API; if hosted, the "resume never leaves the device" promise changes and must be disclosed prominently
5. **Observability** — parse-confidence distributions, gate-pass rates, and validator flag rates are the natural health metrics; log them per run

## Data & Privacy Notes

- `data/` and `uploads/` contain personal data — gitignored; never commit real resumes to a public repo
- `.env` files are local-only; `backend/database/.env.example` documents required variables
- The events DB stores job outcomes tied to a user — in any multi-user deployment this needs a retention policy and export/delete endpoints (GDPR-style) before launch
