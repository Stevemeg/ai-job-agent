# Matching Engine (`backend/matching_engine/`)

Turns 3,875 raw jobs into an explainable ranked list.

## Pipeline

`rank_jobs(profile, jobs)` in `matcher.py`: **dedup → hard gates → weighted score → explain → sort**.

## Modules

| Module | Responsibility |
|---|---|
| `matcher.py` | Orchestrates the pipeline; emits `ranked_jobs.json` records. |
| `scorer.py` | Hard gates + the weighted structured score. |
| `requirement_extractor.py` | Structured JD parsing: **required vs. preferred** years (only *required* gates), clearance, sponsorship language. |
| `skill_taxonomy.py` | Mode A: exact/alias matching, credit 1.0 (RAG ↔ Retrieval-Augmented Generation, LLM ↔ Large Language Model…). |
| `skill_categories.py` | Mode B: category matching, credit 0.6 (CLIP → "vision-language model", GANs → "adversarial training"). |
| `explainer.py` | Per-job `strong_matches`, `likely_matches`, `missing_skills`. |
| `role_scorer.py` | Title relevance to the candidate's track. |
| `domain_filter.py` | Candidate domain detection (AI/ML, data science, web dev). |

## Hard Gates

A job failing any gate is **removed**, never down-ranked:

| Gate | Rationale | Real removal rate |
|---|---|---|
| Title keywords (sales, recruiter, PM…) | Wrong vertical entirely | 13.1% |
| Required years > 3 (fresher) | *Preferred* language deliberately does NOT gate | 54.3% |
| Security clearance | Not obtainable by the candidate | 0.4% |
| No sponsorship | Candidate needs sponsorship (India → US) | 0.1% |

Passing: **32.1% (1,245 / 3,875)**.

## Scoring Formula

```
score = 0.40·skill_overlap + 0.30·semantic + 0.20·role + 0.10·seniority
```

- **skill_overlap (0.40)** — most defensible signal; two-layer matching (Mode A full credit, Mode B 0.6 credit)
- **semantic (0.30)** — `all-MiniLM-L6-v2` cosine similarity between candidate text and JD; catches phrasing keywords miss
- **role (0.20)** — title relevance
- **seniority (0.10)** — fresher/senior alignment

Weights are hand-tuned and live in `config.py`. They are explicitly scheduled for replacement by learning-to-rank once ~50+ real outcome events exist (see the events schema in [JOB_ENGINE.md](JOB_ENGINE.md)).

## Honest-Scoring Notes

- Observed `skill_overlap` values are low (0.09–0.20) on this corpus. That is a *data characteristic*, not a bug: large-company JDs write "AI experience" rather than tool names. The semantic sub-score carries that broader signal.
- Category (Mode B) matches are rare by design — 1 job in 1,245 in the current corpus. The alternative (loose category maps) produced false full-credit matches and was removed; the cleanup rationale is documented in `skill_taxonomy.py`.

## Output Record

```json
{
  "title": "AI Engineer - FDE",
  "company": "Databricks",
  "location": "...",
  "match_score": 40.63,
  "score_breakdown": {"skill_overlap": 0.20, "semantic": 0.61, "role": 1.0, "seniority": 1.0},
  "strong_matches": ["LLM", "RAG", "Prompt Engineering"],
  "likely_matches": [],
  "missing_skills": ["Kubernetes", "Spark"],
  "apply_link": "https://..."
}
```
