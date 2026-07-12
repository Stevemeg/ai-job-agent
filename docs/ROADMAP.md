# Roadmap

## ✅ Completed

- **Phase 1 — Legal ATS sourcing:** Greenhouse + Ashby collectors, dedup, 3,875-job corpus
- **Phase 2 — Events infrastructure:** Postgres 16 (Docker), users/canonical_jobs/events schema, impression + outcome logging with score snapshots
- **Phase 3 — Structured requirement extraction:** required vs. preferred years, clearance, sponsorship — all as hard gates
- **Phase 4 — Two-layer skill matching:** exact/alias (Mode A, credit 1.0) + category (Mode B, credit 0.6)
- **Phase 5 — Resume tailoring:** Ollama/Mistral → ATS-safe .docx via Node docx.js
- **Phase 6 — Truthfulness enforcement:** deterministic hallucination validator with original-bullet fallback
- **Phase 7 — Cover letter generator:** same pipeline + per-paragraph validation
- **Phase 8 — Product UI:** upload-first Streamlit app; parse confidence; Review & Edit; explainable health score; role-weighted skill gaps with statistical honesty guard; career fits with evidence + missing signals; personalized dashboard hero; job cards with tailor/cover-letter entry points
- **Phase 9 — Engineering maturity (v1.0):** Application Tracker (event-sourced, in-app); 68-test suite in CI; FastAPI service layer (13 `/v1` endpoints); Docker Compose full stack; **in-app document generation** with truthfulness reports and downloads; **dual LLM backend** — Ollama for local dev, Groq (`llama-3.3-70b-versatile`) for deployment, auto-selected on `GROQ_API_KEY`; parser robustness (no-duration layouts, Wingdings/private-use bullets, institution guards); release audit + full documentation set

## 🔄 In Progress

- **Outcome logging at scale** — applying to ranked jobs and logging results via `log_outcome_cli`; this is the prerequisite for everything in "learning" below

## 📋 Planned — v1.1

1. ~~**Test suite**~~ — ✅ done: 49 pytest cases (analysis, validator, tracker, taxonomy, regressions) wired into CI. Still to grow: parser tests over diverse resume fixtures, matcher integration tests
2. **Parser upgrades** — experience & certification extraction; project-boundary fix (the known bleed bug parse-confidence already flags)
3. ~~**FastAPI service layer**~~ — ✅ done: 13 `/v1` endpoints with contract tests ([API.md](API.md)). Still to add: document-generation endpoints (need Ollama + async jobs), auth
4. ~~**Application tracker UI**~~ — ✅ done: dashboard Tracker tab (funnel, logging with notes, timeline) over the existing events table
5. **Corpus growth** — more Greenhouse/Ashby slugs; Adzuna API; Lever re-enable per verified slug
6. **Resume A/B suggestions** — "current bullet → suggested rewrite" pairs (validated, of course) rather than advice text

## 🔮 Future

- **Learning-to-rank** — replace hand-tuned weights (0.40/0.30/0.20/0.10) with a model trained on outcome events; needs ~50+ real outcomes; events schema was designed for this from day one
- **Multi-domain taxonomies** — skill databases + role archetypes for mechanical/civil/electrical/HR/marketing/finance (architecture is data-driven; this is content work, not code work)
- **Multi-user SaaS** — profiles to Postgres, auth, per-user ranking caches, precomputed corpus embeddings
- **Auto-apply agent** — browser agent fills ATS forms with tailored documents behind a mandatory human-review gate

## Long-Term Vision

A candidate uploads one resume and receives: an honest assessment, a market-calibrated learning plan, a ranked and explained job list, truthful tailored documents, and (with consent, per application) automated submission — across every professional domain, with every number in the product traceable to real data.
