# Universal AI Job Acquisition Agent — v1.0.0

*Ready to paste into the GitHub release form once the Docker smoke test and screenshots land.*

---

An explainable AI career platform: parse your resume, get an honest analysis, find skill gaps computed from real market data, rank thousands of legally-sourced jobs against your profile, generate truthful tailored documents, and track every application.

## Highlights

- **Resume Intelligence** — PDF parsing with per-section parse confidence, a Review & Edit step, and a deterministic 0–100 health score where every point is traceable to a finding
- **Explainable Job Matching** — 3,875 real jobs from Greenhouse/Ashby board APIs; hard gates (years, clearance, sponsorship, title) then `0.40·skill + 0.30·semantic + 0.20·role + 0.10·seniority`, with strong/likely/missing skills shown per job
- **Market-Driven Skill Gaps** — demand computed from the collected corpus, weighted by target roles, with a statistical honesty guard for small samples
- **Truthful Document Generation, in-app** — one click on any job card produces a tailored resume or cover letter, gated by a deterministic hallucination validator that falls back to original bullets when the LLM fabricates, with a visible per-project truthfulness report and download button
- **Dual LLM backend** — Ollama/Mistral locally (fully private), **Groq `llama-3.3-70b-versatile`** in deployment, auto-selected via `GROQ_API_KEY` in Streamlit Secrets; identical prompts and validation either way
- **Application Tracker** — event-sourced pipeline (saved → applied → interview → offer) with derived status, notes, and timelines; every event snapshots the score for future learning-to-rank
- **REST API** — 13 `/v1` endpoints, auto-docs at `/docs`, versioned responses
- **One-command stack** — `docker compose up --build` starts Postgres + API + UI
- **68 behavior-focused tests** in CI, plus a published [release-readiness audit](RELEASE_AUDIT_V1.md)

## Known limitations

- Single-user, local-first by design — authentication and multi-user are v1.1 (see the audit's Out-of-Scope section)
- Project extraction can merge boundaries on some resume layouts; parse confidence flags it and Review & Edit corrects it
- Document generation needs an LLM backend: local Ollama (private) or a Groq API key (hosted — resume content is sent to Groq; disclosed in DEPLOYMENT.md)
- Fresh clones start from the bundled 120-job sample corpus; run the collector for the full corpus

## Breaking changes

None — first stable release.

## Roadmap

**v1.1:** authentication, multi-user profiles, document-generation API endpoints, structured logging
**v2.0:** cloud deployment / hosted demo, learning-to-rank from accumulated outcomes, additional legal job sources (Adzuna), multi-domain skill taxonomies
