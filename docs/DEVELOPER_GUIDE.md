# Developer Guide

## Environment Setup

```bash
git clone https://github.com/Stevemeg/universal-ai-job-agent.git
cd universal-ai-job-agent
python -m venv venv
# Windows PowerShell:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\venv\Scripts\Activate.ps1
# Linux/macOS:
source venv/bin/activate

pip install -r requirements.txt
```

Optional components:

```bash
ollama pull mistral                                # tailoring + cover letters (~4.4 GB)
cd backend/database && docker compose up -d && cd ../..   # outcome logging (Postgres 16)
# Node deps for docx generation live in backend/resume_engine/node_modules (docx 9.x)
```

## Folder-by-Folder

| Path | Purpose | Touch it when… |
|---|---|---|
| `backend/config.py` | All paths, scoring weights, hard-filter gates | Changing weights or gates — never hardcode paths elsewhere |
| `backend/version.py` | App + formula version (single source) | Cutting a release or changing scoring weights |
| `backend/llm/` | Provider abstraction: Groq (deploy) / Ollama (dev), auto-selected on `GROQ_API_KEY` | Adding an LLM backend — engines only ever call `generate(prompt)` |
| `backend/api/` | FastAPI `/v1` — routers hold zero business logic | Exposing an engine function over HTTP |
| `backend/analysis/` | Pure career-intelligence functions | Adding insight features (new score, new gap logic) |
| `backend/matching_engine/` | Gates, scorer, taxonomy, explainer | Improving match quality |
| `backend/resume_parser/` | PDF → profile (`api.py` is the only public entry) | Improving extraction |
| `backend/job_scraper/` | Collectors, dedup, ranking scripts, diagnostics | Adding job sources |
| `backend/resume_engine/` | Ollama tailoring, validator, docx generation | Document-generation features |
| `backend/database/` | Postgres schema, events, Docker Compose | Event/outcome features |
| `backend/ui/` | `app.py` (router) + `views/` + `theme.py` | UI only — no business logic here, ever |
| `data/` | Corpus, profile, ranked output, generated docs | Gitignore user data in public forks |
| `datasets/` | `skills_database.csv` (skill, category, domain) | Adding recognizable skills |
| `docs/` | You are here | — |

## Everyday Commands

```bash
# Always from project root
python -m backend.job_scraper.ats_collector        # collect
python -m backend.job_scraper.dedupe_jobs          # dedup
python -m backend.job_scraper.run_real_ranking     # rank + save ranked_jobs.json (--log for Postgres)
streamlit run backend/ui/app.py                    # UI (docs generation also in-app)
uvicorn backend.api.app:app --reload               # REST API (/docs)
pytest tests/ -q                                   # 68 tests
python -m backend.resume_engine.resume_tailor --rank 1   # LLM auto-selects:
python -m backend.resume_engine.cover_letter --rank 1    # Ollama local / Groq if key set
python -m backend.resume_engine.validate_tailored  # validator smoke test
python -m backend.database.log_outcome_cli         # log outcomes
python -m backend.job_scraper.gate_breakdown       # diagnostics
```

## Architecture Rules (enforced by review)

1. **No business logic in `backend/ui/`.** Views render; `analysis/` computes.
2. **All paths through `config.py`.** The pipeline must run from any CWD.
3. **Gates remove, scores rank.** Never convert a gate into a score penalty.
4. **Every user-facing number must be explainable.** If you add a score, add its `findings`/evidence too.
5. **LLM output is never trusted.** Anything generated must pass `validate_tailored` (or an equivalent deterministic check) before reaching a document.
6. **Real numbers only in docs.** Update RESULTS.md from actual runs; never estimate.

## Common Extension Recipes

**Add a job source:** new collector in `job_scraper/` emitting the `jobs.json` record shape → run dedup. Legal APIs only.

**Add a role archetype (new domain):** add an entry to `ROLE_ARCHETYPES` and `ROLE_TITLE_KEYWORDS` in `analysis/career_recommender.py`. No code changes.

**Add a market skill:** add `"Skill": r"\bregex\b"` to `MARKET_SKILLS` in `analysis/skill_gap.py`. Word-boundary regexes; beware short tokens (see the `Go` false-positive comment — `\bgo\b` measured 26.1% fake demand vs. 0.9% real).

**Add a health dimension:** new `_score_x(profile) -> (pts, findings)` in `analysis/resume_health.py`, register in `compute_health` dims, add its explanation string to `HEALTH_EXPLANATIONS` in `ui/views/analysis.py`.

## Gotchas

- **Hot-reload is off** (`fileWatcherType = "none"` in `.streamlit/config.toml`): Streamlit's watcher walks all imported modules and detonates `transformers`' lazy imports (missing-torchvision traceback spam) after an in-app re-rank. Restart the app after UI edits, or develop with `streamlit run backend/ui/app.py --server.fileWatcherType auto` *before* triggering a re-rank.

- **Windows + OneDrive:** stale `__pycache__` can outlive source edits (OneDrive mtime quirks). Mystery `SyntaxError` on a file that looks fine → delete `__pycache__` directories and rerun.
- **Streamlit runs `app.py` as a script**, not a package — `app.py` injects the project root into `sys.path`; keep imports in `ui/` absolute (`from backend.ui import theme`).
- **First ranking run is slow:** the sentence-transformer downloads/loads, then 3,875 encodes. Don't Ctrl+C; subsequent UI loads read the JSON instantly.
- **`--rank N` refers to the saved, unfiltered ranking** — the UI job cards display the correct overall rank for copy-paste.

## Testing

No formal suite yet (top of the quality roadmap — see [ROADMAP.md](ROADMAP.md)). Until then: `analysis/` functions are pure — test with JSON fixtures; `validate_tailored.py` self-tests via `python -m backend.resume_engine.validate_tailored`.
