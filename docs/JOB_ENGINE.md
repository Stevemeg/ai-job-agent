# Job Engine (`backend/job_scraper/` + `backend/database/`)

## Job Collection — Legal by Design

Jobs come exclusively from **public ATS board APIs** that companies intentionally expose:

| Source | Status | Notes |
|---|---|---|
| Greenhouse (`boards-api.greenhouse.io`) | ✅ Active | Seed list of company slugs in `ats_collector.py` |
| Ashby (`api.ashbyhq.com/posting-api`) | ✅ Active | Same pattern |
| Lever | ⏸ Disabled | 11/11 attempted slugs returned 404; re-enable per-company when a verified slug is found in a careers URL |
| LinkedIn / Indeed / Naukri | ❌ Deliberately excluded | Scraping violates their ToS. This is a feature of the project, not a gap. |

Pipeline: `ats_collector.py` → `dedupe_jobs.py` (canonical key: lowercased company + whitespace-normalized title) → `data/jobs.json`.

Current corpus: **3,875 deduplicated jobs**.

## Ranking Entry Point

`run_real_ranking.py` — ranks the full corpus against `candidate_profile.json`, prints the top matches with score breakdowns, saves **`data/ranked_jobs.json`** (consumed by the UI, resume tailor, and cover-letter generator), and optionally logs impressions with `--log`.

## Diagnostics

Purpose-built inspection scripts (useful in code review — they document how the system was debugged):

| Script | Question it answers |
|---|---|
| `gate_breakdown.py` | Which gate removes how many jobs? |
| `sample_years_gate.py` | What does the years gate actually match? |
| `check_likely_matches.py` | Does Mode B fire on real data? |
| `inspect_likely_match.py` | Show the matched JD context |

## Events Database (`backend/database/`)

PostgreSQL 16 via Docker Compose. Schema: `users → events → canonical_jobs`.

**Key design decision:** every event snapshots `match_score` and `score_breakdown` *at logging time*. Scoring weights will change (learning-to-rank is planned); historical training data must not silently change with them.

| Component | Purpose |
|---|---|
| `schema.sql` | Tables: users, canonical_jobs, events |
| `db.py` | SQLAlchemy models + session management |
| `events.py` | `log_impressions()`, `log_outcome()` |
| `log_outcome_cli.py` | Interactive outcome logging: applied / interview / rejected / offer / save / dismiss |
| `docker-compose.yml` | Postgres 16 |

Event types double as future training labels: an `offer` is a stronger positive than an `interview`, which is stronger than an `applied`; `dismiss` is a negative.

## Extending the Corpus

1. Add company slugs to the Greenhouse/Ashby seed lists in `ats_collector.py`.
2. Re-run collector + dedup.
3. Planned: Adzuna API for broader coverage (has an official free tier — stays within the legal-sourcing principle).
