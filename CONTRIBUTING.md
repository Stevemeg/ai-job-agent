# Contributing

Thanks for your interest! This project values **explainability and honesty over features** — contributions are reviewed against that bar.

## Ground Rules

1. **No scraping sources that prohibit it.** Job collectors must use official/public APIs (Greenhouse, Ashby, Adzuna-style). PRs adding LinkedIn/Indeed/Naukri scrapers will be closed.
2. **Every user-facing number must be explainable.** New scores ship with their evidence/findings, and the UI must be able to show how they're computed.
3. **LLM output is never trusted.** Generated content must pass a deterministic validation step before reaching a document.
4. **No business logic in `backend/ui/`.** Insight code goes in `backend/analysis/`; views only render.
5. **Real numbers in docs.** If your change alters results, update `docs/RESULTS.md` from an actual run.

## Good First Issues

- Add skills to `MARKET_SKILLS` (`backend/analysis/skill_gap.py`) — mind word-boundary regexes; see the `Go` false-positive case study in that file
- Add role archetypes for new domains (`backend/analysis/career_recommender.py`) — pure data, no code
- Add Greenhouse/Ashby company slugs (`backend/job_scraper/ats_collector.py`)
- Expand `datasets/skills_database.csv` for non-AI domains
- Write pytest cases for `backend/analysis/` (pure functions — easy to test)

## Workflow

1. Fork → branch (`feat/...`, `fix/...`, `docs/...`)
2. Make the change; run relevant module smoke tests (e.g. `python -m backend.resume_engine.validate_tailored`)
3. PR with: what changed, why, and real before/after output where applicable
4. One feature per PR

## Setup

See [docs/DEVELOPER_GUIDE.md](docs/DEVELOPER_GUIDE.md).

## Code Style

Python 3.11, type hints on new public functions, module docstrings that explain *why* (see existing modules for the house style — design rationale lives in the code, not just commit messages).
