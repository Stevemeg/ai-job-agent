# API (FastAPI — implemented)

> **Status: implemented** for profile, analysis, rankings, and tracker (`backend/api/`). Document-generation endpoints (tailored resume, cover letter) remain CLI-only for now — they need Ollama and are the next candidates for async endpoints.

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
| POST | `/v1/rankings` | `services.ranking.request_ranking` → worker runs `matching_engine.matcher.rank_jobs` | Queue a ranking run (202). 409 while one is active; 503 without a database ([details](#rankings-on-the-task-queue)) |
| GET | `/v1/rankings/status` | `services.ranking.status_from_task` | Poll ranking state |
| GET | `/v1/rankings` | — | Ranked results (limit/offset/min_score) |
| GET | `/v1/tracker` | `database.tracker.get_tracked_jobs` | Tracked applications with derived status |
| POST | `/v1/tracker/events` | `database.events.log_outcome` | Log save/applied/interview/rejected/offer/dismiss |

## Rankings on the task queue

Ranking takes minutes, so it runs on the durable worker ([ADR-001 §2](adr/ADR-001-v2-always-on-architecture.md)), not in the API process. The contract from v1 is unchanged: `POST` returns `202`, clients poll `/status`, and `GET /v1/rankings` serves the same payload. What changed is where the work runs.

`POST /v1/rankings` enqueues a `rank` task in the Postgres `tasks` table with `concurrency_key = "rank:default"`. The worker (`python -m backend.worker`, see [DEPLOYMENT.md](DEPLOYMENT.md#task-worker)) claims it, ranks the profile against the **database** corpus, and writes `data/ranked_jobs.json`. It is the only writer of that file.

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
