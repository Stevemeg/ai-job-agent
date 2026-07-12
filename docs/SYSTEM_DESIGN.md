# System Design

## Layered Architecture

```
Presentation   backend/ui/          Streamlit router + views + theme
               backend/api/         FastAPI /v1 (routers hold no logic)
Domain         backend/analysis/    career intelligence (pure functions)
               backend/matching_engine/
               backend/resume_parser/
               backend/resume_engine/  tailoring + validation + documents
Infrastructure backend/llm/         provider abstraction: Groq (prod) / Ollama (dev)
               backend/job_scraper/ collectors, ranking scripts
               backend/database/    Postgres events + tracker queries
Shared         backend/config.py    paths, weights, gates
               backend/version.py   app + formula version
```

**Dependency rules:** Presentation → Domain → Infrastructure → config; Domain never imports Presentation; engines call `llm.generate(prompt)` and never know the provider. Proven in practice: FastAPI was added without touching a scoring function, and the Ollama→Groq deployment swap touched zero business logic.

## Key Design Decisions & Trade-offs

| Decision | Alternative rejected | Why |
|---|---|---|
| Deterministic health/fit scoring | LLM grader | Reproducible, free, instant, explainable; LLM scores vary run-to-run and can't justify themselves |
| Hard gates remove jobs | Down-ranking | A clearance-required job isn't a "worse match", it's not an option; down-ranking wastes ranked slots and user trust |
| Two-tier skill credit (1.0 / 0.6) | Flat matching | "Vision-language models" in a JD is weaker evidence than "CLIP"; flat credit produced dishonest scores |
| ATS board APIs only | Scraping LinkedIn/Indeed | ToS compliance is non-negotiable for a public project |
| Validator after LLM | Stronger prompts only | Prompts lower hallucination probability; validation makes it detectable. Both are used. |
| Provider abstraction (Groq prod / Ollama dev) | Hardcoding one LLM | Deployed apps can't reach a developer's local Ollama; local dev shouldn't require an API key. Auto-selection on `GROQ_API_KEY` gives both, with identical prompts and validation. |
| Events snapshot scores | Join to live scores | Weight changes must not corrupt historical training data |
| Session-state stage machine (Streamlit) | Streamlit multipage | Multipage exposes all pages in the sidebar, breaking the upload-first flow |
| JSON files for corpus/profile | Postgres for everything | Right-sized: single-user local tool today; events (the append-only, queryable part) already live in Postgres |

## Data Stores

| Store | Contents | Why this store |
|---|---|---|
| `data/jobs.json` | 3,875 deduplicated jobs | Read-mostly corpus, reloaded per run |
| `data/candidate_profile.json` | Parsed + user-corrected profile (with `.bak` backup on overwrite) | Single-user document |
| `data/ranked_jobs.json` | Ranked output snapshot | Decouples slow ranking (minutes) from instant UI |
| PostgreSQL | users, canonical_jobs, events | Append-only outcome log → future training data |

## Performance Characteristics

| Operation | Cost | Mitigation |
|---|---|---|
| Ranking 3,875 jobs | Minutes (embedding model load + encode) | Never runs implicitly; explicit button; output cached to `ranked_jobs.json` |
| Analysis (health/gaps/careers/suggestions) | < ~2 s over 3,875 jobs | Single `st.cache_data` call keyed on profile + corpus mtime |
| Resume parsing | Seconds | Runs once per upload |
| LLM tailoring | ~1–2 min (local Mistral) | CLI-invoked, explicitly |

## Scalability Path (single-user → SaaS)

1. **Now:** local single-user; one profile; JSON + Postgres.
2. **FastAPI layer:** expose `analysis/` + `matching_engine/` as endpoints (`/parse-resume`, `/health`, `/gaps`, `/rank`); Streamlit becomes one client.
3. **Multi-user:** profiles move from JSON file to Postgres (users table already exists); per-user ranked caches.
4. **Scale ranking:** precompute job embeddings once per corpus refresh (not per user); store in a vector index; per-user ranking becomes one encode + ANN lookup.
5. **Learning-to-rank:** replace hand-tuned weights with a model trained on accumulated events (needs ~50+ outcomes to be meaningful).

## Failure Modes & Graceful Degradation

- Postgres down → ranking still works; `--log` fails with a clear message (never crashes the run)
- Ollama down → tailoring exits with instructions; ranking/UI unaffected
- No `ranked_jobs.json` → UI offers explicit "Rank jobs now"; tailor falls back to inline ranking
- Un-parseable PDF → landing shows the error; nothing downstream runs on garbage
- LLM hallucination → validator flags; project falls back to original bullets
