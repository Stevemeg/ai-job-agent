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
| POST | `/v1/rankings` | `matching_engine.matcher.rank_jobs` | Start background ranking run (202) |
| GET | `/v1/rankings/status` | — | Poll ranking state |
| GET | `/v1/rankings` | — | Ranked results (limit/offset/min_score) |
| GET | `/v1/tracker` | `database.tracker.get_tracked_jobs` | Tracked applications with derived status |
| POST | `/v1/tracker/events` | `database.events.log_outcome` | Log save/applied/interview/rejected/offer/dismiss |

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

- Ranking and document generation are **async jobs** (returns 202 + job id) — they take minutes and must not block a request thread.
- Auth: single-user today (`users` table already exists); JWT planned with multi-user.
- Versioning: `/v1` prefix from day one; score-formula changes bump a `formula_version` field in responses (the events table already snapshots scores for exactly this reason).
