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
- **Ranking:** requirements.txt includes torch/sentence-transformers — the build is heavy but works on Community Cloud; first ranking run is slow. Prefer committing a pre-ranked `ranked_jobs.json` or using the sample corpus for demos.
- **.docx generation needs Node.js**: add a `packages.txt` containing `nodejs` and `npm`, and the docx npm package must be installable (`backend/resume_engine/node_modules` is gitignored). Until wired, cover letters fall back to .txt gracefully; resume tailoring reports the error. A python-docx fallback is the planned proper fix.
- **Tracker needs Postgres**: point `DB_*` env at a hosted Postgres (e.g. Neon/Supabase free tier) via secrets, or the Tracker tab shows its graceful "unreachable" panel.

## Current Model: Local-First

The system runs entirely on the user's machine by design — resume data is sensitive, and local Ollama inference means **no resume content ever leaves the device**.

| Component | Runtime | Required for |
|---|---|---|
| Python 3.11 + venv | Always | Everything |
| Streamlit | `streamlit run backend/ui/app.py` | UI |
| Ollama + Mistral | `ollama serve` | Tailoring, cover letters |
| PostgreSQL 16 | Docker Compose | Outcome logging only |
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
```

One shared image serves both the API and UI (same engines, different command). Design notes:

- `./data` and `./uploads` are **bind-mounted** — profiles, corpora, and rankings persist on the host and stay shared with host-side CLI runs
- The embedding model caches in a named `models` volume (`HF_HOME=/models`) — the ~90 MB download happens once, not per container
- The API waits for Postgres via healthcheck (`condition: service_healthy`); its own `/healthz` backs the container healthcheck
- `requirements-docker.txt` is a curated runtime set, not the dev-machine freeze — smaller, Linux-clean image
- **Ollama stays host-side** (resume tailoring / cover letters are CLI workflows); `backend/database/docker-compose.yml` still works standalone for DB-only development
- Credentials override via `.env` at the repo root (`DB_USER` / `DB_PASSWORD` / `DB_NAME`)

## Single-Host Server Deployment (small-team demo)

```
[Caddy/nginx TLS] → [docker compose up]   (UI 8501, API 8000, Postgres internal)
                     [ollama serve]        (host, GPU strongly recommended)
```

Add before exposing publicly: an auth proxy in front of Streamlit (it has no auth), per-user profile isolation (currently single-profile), and upload size limits.

## Production SaaS Path (planned)

1. **API extraction** — FastAPI over `analysis/` + `matching_engine/` ([API.md](API.md)); Streamlit (later React) becomes a client
2. **Workers** — ranking and LLM generation move to a task queue (Celery/RQ); API returns job ids
3. **Storage** — profiles → Postgres; job corpus → Postgres + precomputed embedding index (encode corpus once per refresh, not per user)
4. **LLM serving** — pool of Ollama instances or a hosted-model API; if hosted, the "resume never leaves the device" promise changes and must be disclosed prominently
5. **Observability** — parse-confidence distributions, gate-pass rates, and validator flag rates are the natural health metrics; log them per run

## Data & Privacy Notes

- `data/` and `uploads/` contain personal data — gitignored; never commit real resumes to a public repo
- `.env` files are local-only; `backend/database/.env.example` documents required variables
- The events DB stores job outcomes tied to a user — in any multi-user deployment this needs a retention policy and export/delete endpoints (GDPR-style) before launch
