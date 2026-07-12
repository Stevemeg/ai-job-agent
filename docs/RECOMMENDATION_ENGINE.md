# Recommendation Engine (`backend/analysis/`)

Pure functions over `candidate_profile.json` + `jobs.json`. No Streamlit, no network, no LLM, no side effects — instantly testable, and directly exposable as FastAPI endpoints later.

## resume_health.py — Resume Health Score

Deterministic 0–100 across six weighted dimensions:

| Dimension | Max | Computed from |
|---|---|---|
| Contact & Links | 15 | name/email/phone/LinkedIn/GitHub presence (3/4/3/3/2) |
| Skills | 20 | breadth (≤15 skills → 12 pts) + category diversity (≤8 categories → 8 pts) |
| Projects | 20 | count (8) + real titles (4) + ≥3 bullets each (8) |
| Quantified Impact | 15 | share of bullets containing a number/%/metric |
| Education | 10 | degree (4) + institution (3) + years (2) + GPA (1) |
| ATS Readiness | 20 | action-verb bullet starts (8) + bullet length 8–40 words (6) + section completeness (6) |

Every dimension returns `findings` — the concrete evidence for lost points — and the UI shows the formula per dimension. **Why not an LLM grader?** Different number every run, costs tokens, can't explain itself. Deterministic scoring is instant, free, reproducible, and auditable.

## skill_gap.py — Market-Driven Skill Gaps

`impact of a missing skill = share of jobs mentioning it`, computed from the project's own corpus — never invented percentages.

- `MARKET_SKILLS`: ~30 curated high-frequency skills with word-boundary regexes (extending domains = adding entries, not code)
- **Role weighting:** demand is computed within the subset of jobs whose titles match the candidate's top-3 career fits — 12% overall demand can hide much higher demand in target roles
- **Statistical honesty guard:** role-weighted percentages are only headlined when the role subset has ≥ 30 jobs; below that, the UI falls back to overall demand rather than presenting noise as insight
- Regex discipline matters: the naive `\bgo\b` pattern inflated Go demand to a false 26.1%; the corrected pattern (`golang` / "go developer|engineer|programming") measures the true 0.9%

## career_recommender.py — Role Fit

Keyword-archetype coverage, deliberately not embeddings, because the output must be explainable:

- 8 role archetypes (AI Engineer, ML Engineer, LLM/GenAI, CV, NLP, Data Scientist, Backend, Data Analyst), each with `core` and `bonus` signal lists
- `fit = 0.75·core_coverage + 0.25·bonus_coverage`
- Output includes `evidence` (matched signals — shown as "✓ You have") **and** `missing` (unmatched core signals — shown as "To strengthen")
- Archetypes are data: supporting Mechanical/Civil/HR/Finance means adding dict entries

## suggestions.py — Evidence-Backed Suggestions

Rules that quote the resume itself: unquantified bullets (with the worst offender quoted), weak opening verbs (quoted), missing deployment story, top market gaps (with corpus percentages), missing profile links. Severity-sorted. Never generic.

## parse_confidence.py

Extraction-quality scoring — documented in [RESUME_ENGINE.md](RESUME_ENGINE.md#parse-confidence-backendanalysisparse_confidencepy).

## Testing These Modules

All are pure — test with plain JSON fixtures:

```python
from backend.analysis.resume_health import compute_health
health = compute_health(json.load(open("data/candidate_profile.json")))
assert 0 <= health["score"] <= 100
```
