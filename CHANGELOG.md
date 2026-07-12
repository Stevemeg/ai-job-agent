# Changelog

All notable changes to this project. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/) once releases begin.

## [1.0.0] — 2026-07-13

First stable release. Everything below shipped between 0.4.0 and 1.0.0.

### Added
- v1.0 release-readiness audit ([docs/RELEASE_AUDIT_V1.md](docs/RELEASE_AUDIT_V1.md)): 0 critical, 4 high (3 fixed), 8 medium (3 fixed) findings

### Fixed
- **Parser: projects without duration lines extracted zero projects** (live failure on a real resume). Segmentation is now title-driven when no dates exist: a title-like line followed by bullets starts a project. Checkmark (✓/✔) and arrow bullets recognized. Bullet separators (•) now preserved in descriptions so downstream bullet counting works.
- **Parser: education grabbed arbitrary nearby prose as the institution** (live failure: institution = "Docker command."). Institution now requires an institution keyword (institute/university/college/…), same-line "College — Degree" layouts are split correctly, and section-header lines are never candidates. Honest `None` when nothing matches. 8 new regression tests (68 total).
- White-on-white text for users whose Streamlit defaulted to the dark theme: the palette now lives in `.streamlit/config.toml` (`base = "light"`) so all native widgets follow it; theme.py no longer forces background/text colors via CSS. Upload size capped at 10 MB in the UI to match the API.

### Fixed (audit pass)
- Impressions and outcomes now share one user identity (was split across a demo user and the profile user — would have poisoned learning-to-rank labels)
- `requirements.txt` re-encoded UTF-16 → UTF-8 (fresh-clone blocker on Linux/macOS)
- `call_ollama` raises `OllamaUnavailableError` instead of `sys.exit(1)` in library code
- `init_db()` memoized per process; API caches the job corpus on file mtime; 10 MB upload cap on `POST /v1/resumes`
- **Dockerized full stack**: root `docker-compose.yml` starts Postgres + FastAPI + Streamlit with one command; shared image (`Dockerfile`), curated `requirements-docker.txt` (not the dev freeze), bind-mounted `data/`/`uploads/`, named volume for the embedding-model cache, healthcheck-gated startup ordering. `.dockerignore` keeps personal data and dev artifacts out of the image.
- **FastAPI service layer** (`backend/api/`): 13 endpoints under `/v1` over the existing engines — resume upload/parse, profile CRUD, health/gaps/careers/suggestions, background ranking runs with status polling, tracker read/write. Routers contain zero business logic; heavy imports (torch, PyMuPDF, SQLAlchemy) are deferred so the API boots instantly and degrades per-route when Postgres/ML deps are absent. Auto-docs at `/docs`; `/healthz` exposes `formula_version`. 11 contract tests (60 total).
- **LLM provider abstraction (`backend/llm/`)**: engines call `generate(prompt)` and never know the backend. Groq (`llama-3.3-70b-versatile`, production tier, JSON mode) auto-selected when `GROQ_API_KEY` exists in Streamlit Secrets or env; Ollama (mistral) otherwise. Prompts, hallucination validator, validation reports, and exports unchanged. Missing key + no Ollama → friendly in-app message, never a crash. New `CONFIGURATION.md`; `.streamlit/secrets.toml` gitignored; compose passes `GROQ_API_KEY` through.
- **In-app document generation**: "Optimize resume" and "Cover letter" buttons on job cards now run the full pipeline inside the app (JD analysis → LLM rewrite → hallucination validation → .docx) with a spinner, a per-project truthfulness report, and a download button — no more copy-paste CLI commands. New callable entry points `resume_tailor.tailor_to_job()` and `cover_letter.generate_for_job()` are also ready for future API document endpoints.
- **Test suite: 49 pytest cases** across resume health, skill gaps (incl. the Go-regex regression test and the ≥30-job honesty-guard test), career recommender, parse confidence, suggestions, hallucination validator, tracker status derivation, and skill-taxonomy consistency. CI now runs the suite on every push with a deliberately light dependency install.

### Fixed
- Validator false positive on inflections: "Indexed" now validates against an original saying "indexing" (minimal suffix stemming — deliberately not a full stemmer, which would over-merge and let fabrications through). Found by the new test suite.
- **Application Tracker** (dashboard tab): pipeline funnel (saved/applied/interview/offer/rejected), event logging with job picker from the ranked list, free-text notes, per-job timeline. Derived status (furthest stage reached; terminal events override) — never stored, always consistent with history. Degrades gracefully when Postgres is down.
- `note` column on events (idempotent migration in `db.py:_run_migrations()`; schema.sql updated)
- Full repository documentation: README, docs/ (architecture, engines, system design, API spec, deployment, developer guide, results, roadmap, screenshots guide), community files, CI scaffold

## [0.4.0] — 2026-07 · Product UI

### Added
- Upload-first Streamlit application: landing → parse → **Review & Edit** → dashboard
- Parse-confidence scoring per section with concrete warnings (caught a real project-boundary parser bug)
- Resume Health Score (0–100) with per-dimension formulas and findings shown in the UI
- Role-weighted skill gap analysis with a ≥30-job statistical honesty guard
- Career recommendations with evidence ("✓ You have") and missing core signals ("To strengthen")
- Personalized dashboard hero (strongest roles + biggest opportunity from real corpus data)
- Job cards with score-breakdown bars, JD viewer, apply links, tailor/cover-letter entry points

### Fixed
- `Go` market-skill regex false positive (measured 26.1% fake demand; corrected pattern measures 0.9%)
- Hardcoded `../datasets` path in resume parser (new `resume_parser/api.py` entry point)

## [0.3.0] — 2026-07 · Truthfulness & Documents

### Added
- Deterministic hallucination validator: tailored bullets verified against original resume; fabricated tools/metrics flagged; automatic fallback to original bullets
- Cover letter generator (Ollama) with per-paragraph validation, `.txt` + `.docx` output
- `run_real_ranking` now persists `data/ranked_jobs.json`; tailor/cover-letter load it instead of re-ranking (minutes → instant)

## [0.2.0] — 2026 H1 · Matching & Events

### Added
- Structured requirement extraction: required vs. preferred years, clearance, sponsorship — as hard gates
- Two-layer skill matching: exact/alias (credit 1.0) + category (credit 0.6)
- PostgreSQL events infrastructure with score snapshots at log time
- Resume tailoring via Ollama/Mistral → ATS-safe .docx (Node docx.js)

## [0.1.0] — 2026 H1 · Foundation

### Added
- Resume parser: PDF → structured candidate profile
- Legal ATS job collection (Greenhouse + Ashby), dedup — 99 → 3,875 jobs
- Weighted ranking pipeline (skill/semantic/role/seniority) with explainable output
