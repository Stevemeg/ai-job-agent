# v1.0 Release-Readiness Audit

Staff-engineer-style review of the full repository ahead of tagging v1.0. Findings are prioritized; items marked **FIXED** were resolved in the audit pass itself. Verified by the 60-test suite (green after all fixes).

## Critical

**None found.** No injection vectors (all DB access via SQLAlchemy ORM parameters), no secrets committed (`.env` files gitignored; `.env.example` documents variables), no personal data baked into the Docker image (`.dockerignore` excludes `data/`, `uploads/`), LLM output is deterministically validated before reaching documents.

## High

| # | Finding | Status |
|---|---|---|
| H1 | **Split user identity poisoning future training labels.** `run_real_ranking --log` recorded impressions under a hardcoded demo email while the tracker logs outcomes under the profile's email — the events log would attribute impressions and outcomes to different users, corrupting learning-to-rank data before it exists. | **FIXED** — impressions now use the profile identity via `tracker.get_or_create_user` |
| H2 | **`requirements.txt` was UTF-16.** A Windows `pip freeze >` artifact; `pip install -r` fails on Linux/macOS with an encoding error — a fresh-clone blocker for non-Windows contributors. | **FIXED** — re-encoded UTF-8 (78 pins preserved) |
| H3 | **API is unauthenticated.** Anyone who can reach it can read/overwrite the profile and log events. Acceptable for the documented local-first deployment; a blocker for any public hosting. | **OPEN** — planned Milestone 2 (auth); DEPLOYMENT.md and SECURITY.md already state the constraint |
| H4 | **`sys.exit(1)` inside `call_ollama` (library code).** Would kill the server process when tailoring becomes an API endpoint. | **FIXED** — raises `OllamaUnavailableError`; CLI entry points catch and exit cleanly |

## Medium

| # | Finding | Status |
|---|---|---|
| M1 | **`init_db()` ran `CREATE TABLE` + `ALTER` on every tracker request** (UI render and API call). Wasteful round-trips per request. | **FIXED** — memoized per process |
| M2 | **API re-parsed the 3,875-job corpus JSON on every `/gaps` and `/suggestions` request.** | **FIXED** — cached on `(path, mtime)`; corpus refresh invalidates automatically |
| M3 | **No upload size limit on `POST /v1/resumes`** — whole file read into memory. | **FIXED** — 10 MB cap → 413 |
| M4 | **Fresh clone is empty**: `data/jobs.json` is gitignored, so `docker compose up` starts a working app with zero jobs. Graceful (UI/API both handle it) but underwhelming for evaluators. | **OPEN** — recommend committing a small `data/sample_jobs.json` (~100 sanitized postings) with a first-run banner, or documenting the collector as step 1 (currently done in README) |
| M5 | **Known parser defect**: project descriptions can bleed across boundaries. Mitigated three ways (parse confidence flags it, Review & Edit lets users fix it, health score penalizes empty titles) but the extractor itself is unfixed. | **OPEN** — parser robustness is the top post-1.0 engineering item; needs a labeled multi-resume fixture set first |
| M6 | **No structured logging anywhere** — `print()` throughout CLI paths, nothing in API routes beyond uvicorn access logs. | **OPEN** — Milestone: `logging` with request IDs in the API, one config module; do alongside auth |
| M7 | **Two uncoordinated writers of `ranked_jobs.json`** (API background thread and Streamlit re-rank button). Concurrent runs could interleave writes. Single-user reality makes this improbable; still a real race. | **OPEN** — acceptable for v1.0 (documented single-user scope); task queue solves it properly in the SaaS path |
| M8 | **Default DB credentials in compose/db.py** (`dev_password_change_me`). Fine for local dev, overridable via `.env`, but port 5432 is host-exposed. | **OPEN** — acceptable locally; remove the port mapping in any hosted deployment (documented in DEPLOYMENT.md) |

## Low

| # | Finding | Status |
|---|---|---|
| L1 | `st.cache_data.clear()` after profile save clears all Streamlit caches including the job corpus (slightly slower next load). Correctness over speed — fine. | OPEN (won't fix for 1.0) |
| L2 | Validator conservatism: uncommon capitalized-but-legitimate words can be flagged. By design (flags are shown, never silently applied); inflection false-positives already fixed via light stemming. | By design |
| L3 | No GitHub Actions status badge in README (repo not yet pushed — badge URL unknown). | OPEN — add on first push |
| L4 | UI container has no healthcheck (API and DB do). | OPEN — cosmetic |
| L5 | `rankings._state` is module-global; tests could theoretically leak state across cases. Currently harmless (status-only assertions). | OPEN — revisit with task queue |
| L6 | Diagnostics scripts (`gate_breakdown` etc.) have no tests. They're read-only debugging tools; low value to test. | Won't fix |

## Dimension Summary

| Dimension | Verdict |
|---|---|
| Architecture | **Strong.** Clean layering (Presentation → Domain → config), engines UI-agnostic, API routers logic-free. Dependency rule held throughout the audit. |
| Security | **Good for documented scope** (local-first). H3/M8 gate any public deployment. |
| Testing | **Good.** 60 behavior-focused tests incl. regressions and adversarial cases; gaps: parser (needs fixtures), matcher scoring (needs ML deps in CI or a slim fake-embedding seam). |
| Performance | **Good after M1/M2.** Ranking cost is honest (explicit, cached); analysis <2 s over full corpus. |
| Error handling | **Good after H4.** Graceful degradation verified for: Postgres down, Ollama down, no rankings, no corpus, bad PDF. |
| Documentation | **Strong.** 13 docs files, 10 Mermaid diagrams, real numbers only. Missing: screenshots/GIF (user-side task). |
| Docker / CI | **Good.** Config-validated compose; CI is light and green. Compose not yet live-built (no daemon in the audit environment) — first `docker compose up --build` on a real machine is the outstanding smoke test. |
| Dependency mgmt | **Good after H2.** Curated Docker requirements separate from dev freeze. |

## Out of Scope for v1.0 (conscious decisions, not oversights)

| Item | Why deferred | Revisit when |
|---|---|---|
| Authentication / multi-user | v1.0 is a local-first single-user product; auth adds registration, hashing, JWT, CSRF, route protection, per-user schema + tests — none of which demonstrates the AI/recommendation engineering this project exists to show | Public hosted deployment (v1.1) |
| Task queue / distributed workers | One user, one ranking at a time; a thread suffices and is honest about its scope | Multi-user SaaS |
| Kubernetes / horizontal scaling | Compose covers the actual deployment target | Real traffic exists |
| Auto-apply / browser automation | Each job board has distinct workflows and ToS; substantial standalone project | Post-SaaS, behind a mandatory human-review gate |
| LinkedIn/Indeed/Naukri sourcing | ToS-prohibited scraping — permanent exclusion, not a deferral | Never (official APIs only) |
| Interview simulator, email automation | Feature creep beyond the acquisition pipeline | User demand proves it |

## Post-Audit Addendum (pre-tag updates)

Changes landed after the audit pass, re-verified with the (now 68-test) suite:

- **In-app document generation** replaced the CLI-popover workaround — engines gained callable entry points (`tailor_to_job`, `generate_for_job`); no business-logic changes
- **LLM provider abstraction** (`backend/llm/`): the audit-era assumption "deployment requires the user's local Ollama" no longer holds — Groq (`llama-3.3-70b-versatile`, production tier) serves deployment via `GROQ_API_KEY` in Streamlit Secrets. New security surface reviewed: key read only from Secrets/env, never committed (`.streamlit/secrets.toml` gitignored), never logged, never baked into the Docker image; 401/429 map to friendly non-crashing errors. **Privacy trade-off documented:** the Groq path sends resume content to a hosted API — disclosed in DEPLOYMENT.md and SECURITY.md.
- **Parser robustness fixes** (no-duration project layouts, private-use/Wingdings bullet glyphs, institution keyword guards) partially retire finding M5 — the boundary-bleed defect class now has 8 regression tests; broader resume-format coverage remains the open part
- Chore: `requirements.txt` UTF-8 (H2), split user identity (H1), and `sys.exit` in library code (H4) confirmed fixed in shipped code

## Release Recommendation

**Ship v1.0 after two user-side actions:** (1) a successful `docker compose up --build` smoke test on a real Docker host, and (2) screenshots + demo GIF per `docs/SCREENSHOTS.md`. Everything else open above is post-1.0 backlog — none of it blocks a portfolio release, and H3 (auth) only blocks *hosted* deployment, which is Milestone 3.
