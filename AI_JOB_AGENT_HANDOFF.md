# AI Job Acquisition Agent — Project Handoff Document
**Generated:** July 2026  
**Project root:** `C:\Users\konab\OneDrive\Desktop\Career\projects\AI_JOB_AGENT`  
**Developer:** Kona Bharath Vamshidhar Reddy (graduating B.E. AI/ML, Acharya Institute of Technology, July 2026)

---

## What this project is

A Python-based AI job acquisition agent that:
1. Collects jobs legally from ATS board APIs (Greenhouse, Ashby)
2. Ranks them against the candidate's profile using a structured scorer
3. Logs impressions and outcomes to Postgres for future learning-to-rank
4. Tailors resumes to specific jobs via Ollama/Mistral

**Goal:** Portfolio project + job search tool for the developer's own July 2026 job hunt (AI/ML/Data/Backend vertical).

---

## Project structure

```
AI_JOB_AGENT/
├── backend/
│   ├── config.py                    # Central paths, weights, hard-filter gates
│   ├── embeddings.py                # Single shared SentenceTransformer model
│   ├── matching_engine/
│   │   ├── matcher.py               # Main rank_jobs() pipeline
│   │   ├── scorer.py                # Hard gates + weighted structured score
│   │   ├── explainer.py             # strong_matches / likely_matches / missing_skills
│   │   ├── requirement_extractor.py # Structured JD parsing (required vs preferred years)
│   │   ├── skill_taxonomy.py        # Exact synonym/alias matching (Mode A)
│   │   ├── skill_categories.py      # Category-level matching with 0.6 partial credit (Mode B)
│   │   ├── role_scorer.py           # Title relevance scoring (original, untouched)
│   │   └── domain_filter.py         # Domain detection (original, untouched)
│   ├── resume_parser/
│   │   ├── resume_parser.py         # Main PDF parser
│   │   ├── project_extractor.py     # FIXED: was producing empty titles
│   │   ├── skill_extractor.py       # Skill detection
│   │   └── entity_extractor.py      # Name/email/phone/links
│   ├── job_scraper/
│   │   ├── ats_collector.py         # Greenhouse + Ashby legal ATS sourcing
│   │   ├── job_parser.py            # UPDATED: detect_seniority with Principal/Staff/PhD
│   │   ├── dedupe_jobs.py           # One-time dedup utility
│   │   ├── retag_seniority.py       # One-time seniority re-tagger
│   │   ├── run_real_ranking.py      # Main ranking script (--log flag for Postgres)
│   │   ├── gate_breakdown.py        # Diagnostic: which gate removes how many jobs
│   │   ├── sample_years_gate.py     # Diagnostic: inspect years-gate matches
│   │   ├── check_likely_matches.py  # Diagnostic: confirm Mode B fires in real data
│   │   └── inspect_likely_match.py  # Diagnostic: show actual matched context
│   ├── database/                    # NOTE: folder is named 'database', not 'db'
│   │   ├── docker-compose.yml       # Postgres 16 via Docker
│   │   ├── schema.sql               # users, canonical_jobs, events tables
│   │   ├── db.py                    # SQLAlchemy models + session management
│   │   ├── events.py                # log_impressions() + log_outcome()
│   │   ├── log_outcome_cli.py       # Interactive CLI for manual outcome logging
│   │   └── .env.example             # DB connection env vars
│   └── resume_engine/
│       ├── resume_tailor.py         # Ollama-powered resume tailoring
│       ├── generate_resume_docx.js  # Node.js docx generator (ATS-safe layout)
│       └── node_modules/docx/       # Local docx npm package
├── data/
│   ├── candidate_profile.json       # Parsed resume (22 skills, 3 projects with real titles)
│   ├── jobs.json                    # 3,875 deduplicated jobs from ATS sources
│   └── resume_databricks_*.docx     # Tailored resume outputs
└── uploads/
    └── resume.pdf                   # Source resume
```

---

## How to run things

**Always run from project root** (not from inside backend/):
```powershell
cd C:\Users\konab\OneDrive\Desktop\Career\projects\AI_JOB_AGENT
```

**Rank jobs + log impressions:**
```powershell
python -m backend.job_scraper.run_real_ranking --log
```

**Log a job outcome:**
```powershell
python -m backend.database.log_outcome_cli
# Enter company (lowercase, e.g. 'databricks'), exact title, event type
# Event types: applied / interview / rejected / offer / save / dismiss
```

**Generate a tailored resume:**
```powershell
ollama serve   # in a separate terminal, if not already running
python -m backend.resume_engine.resume_tailor --rank 1
# Output lands in data/ folder as a .docx
```

**Collect new jobs:**
```powershell
python -m backend.job_scraper.ats_collector
python -m backend.job_scraper.dedupe_jobs
```

**Start Postgres (required for --log and log_outcome_cli):**
```powershell
cd backend\database
docker compose up -d
cd ..\..
```

---

## Architecture decisions (the important "why"s)

**Scoring formula:** `0.40 × skill_overlap + 0.30 × semantic + 0.20 × role + 0.10 × seniority`  
Weights are hand-tuned. Will be replaced by learning-to-rank once enough outcome events accumulate.

**Hard gates (remove before scoring, never just down-rank):**
- Title contains sales/recruiter/marketing/etc keywords
- Strict years requirement > 3 (preferred language is NOT a gate)
- Security clearance required
- No sponsorship (candidate needs sponsorship: India → US companies)

**Skill matching has two layers:**
- Mode A (exact/alias): `skill_taxonomy.py` — full credit (1.0). RAG ↔ Retrieval-Augmented Generation, LLM ↔ Large Language Model, etc.
- Mode B (category): `skill_categories.py` — partial credit (0.6). CLIP → "vision-language model", GANs → "adversarial training", etc.

**Job supply:** Legal ATS board APIs only (Greenhouse + Ashby). LinkedIn/Naukri/Indeed scraping deliberately excluded (ToS violations). Lever attempted but 11/11 company slugs failed — disabled pending discovery of verified slugs.

**Events schema:** `users` → `events` → `canonical_jobs`. Events snapshot match_score + score_breakdown at time of logging, so scoring weight changes don't corrupt historical data.

---

## Verified real numbers (last run)

- **Jobs in corpus:** 3,875 (deduplicated)
- **Jobs passing gates:** 1,245 / 3,875 (32.1%)
- **Jobs with skill_overlap > 0:** 394 / 1,245
- **Gate breakdown:** 54.3% years_required_too_high, 32.1% passes, 13.1% title_keyword, 0.4% clearance, 0.1% no_sponsorship
- **Jobs with category-level (Mode B) matches:** 1 / 1,245 ("Researcher, Trustworthy AI" @ OpenAI via "adversarial training")
- **Top match:** AI Engineer - FDE @ Databricks, score 40.63

---

## Known issues and limitations

1. **Mistral hallucinations in resume tailoring:** The model adds tools/platforms (Databricks, LangChain, DSPy) not in the original resume. The prompt has been strengthened with explicit negative examples, but always do a truthfulness review before submitting a tailored resume. This is a Mistral 7B limitation — a larger model would be more reliable.

2. **Lever job source is empty:** All 11 attempted company slugs returned 404. No reliable way to discover Lever-hosted companies from outside the platform. Re-enable when you find a company's Lever board naturally (the slug is in their careers URL).

3. **skill_overlap scores are low (0.09–0.20):** This is partly a data characteristic — these large companies' JDs use broad language ("AI experience") rather than specific tool names. The semantic sub-score (0.30 weight) picks up the broader signal. Not a bug.

4. **run_real_ranking.py doesn't save ranked_jobs.json:** The original main.py does, but the improved script only prints + logs to Postgres. resume_tailor.py works around this by re-ranking inline.

5. **Docker must be manually started:** Docker Desktop doesn't auto-start on Windows. Enable "Start Docker Desktop when you log in" in Docker settings, or run `cd backend\database && docker compose up -d` at the start of each session.

---

## What's been completed (phases)

- ✅ **Phase 1:** Legal ATS job sourcing (99 → 3,875 real jobs)
- ✅ **Phase 2:** Postgres events log, Docker, fully verified end-to-end
- ✅ **Phase 3:** Structured requirement extraction (required vs preferred, clearance, sponsorship gates)
- ✅ **Phase 4A:** Skill alias/synonym matching (Mode A)
- ✅ **Phase 4B:** Category-level skill matching (Mode B, 0.6 partial credit)
- ✅ **Phase 5:** Resume tailoring via Ollama/Mistral → .docx output

---

## What's next (in priority order)

1. **Improve resume tailoring prompt** — reduce Mistral hallucinations further; consider adding a post-processing validation step that checks generated content against the original bullets
2. **Log real outcomes** — apply to jobs and log via `log_outcome_cli.py` using exact titles from `run_real_ranking` output; this is the prerequisite for learning-to-rank
3. **Cover letter generator** — same Ollama pipeline, output as .docx or .txt
4. **Browsable UI** — Streamlit MVP so ranked results are readable outside a console
5. **Learning-to-rank** — replace hand-tuned weights with a model trained on outcome events (needs ~50+ real outcomes before it's meaningful)
6. **Add more companies** — extend Greenhouse/Ashby seed lists; add Adzuna API for broader coverage

---

## Environment

- Python 3.11, venv at `venv/`
- Key Python packages: sentence-transformers, sqlalchemy, psycopg2-binary, requests, PyMuPDF, scikit-learn
- Node.js 24, docx 9.6.1 (local install at `backend/resume_engine/node_modules/`)
- Postgres 16 via Docker Compose (`backend/database/docker-compose.yml`)
- Ollama with `mistral:latest` (4.4GB)
- Embedding model: `all-MiniLM-L6-v2` (cached by HuggingFace locally)

---

## Activate venv (Windows PowerShell)

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
& c:\Users\konab\OneDrive\Desktop\Career\projects\AI_JOB_AGENT\venv\Scripts\Activate.ps1
```
