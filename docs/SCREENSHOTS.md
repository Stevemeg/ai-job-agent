# Screenshot Guide

Capture these with the app running (`streamlit run backend/ui/app.py`), browser at 1440×900, light mode. Save to `assets/` with the exact filenames below (README references them).

| # | File | What to capture | Caption |
|---|---|---|---|
| 1 | `screenshot_landing.png` | Landing page, no resume uploaded — hero + drop zone only | *Upload-first: nothing renders until the system understands you.* |
| 2 | `screenshot_review.png` | Review & Edit page with the ⚠️ warnings expander open, confidence pills visible | *The parser grades its own output — 98% confident, and it tells you exactly what to double-check.* |
| 3 | `screenshot_dashboard.png` | Dashboard Overview tab: hero greeting + score ring + breakdown with one "How is X scored?" expander open | *Every score is explainable — expand any dimension to see the formula and the evidence.* |
| 4 | `screenshot_skills.png` | Skills tab, grouped category cards | *22 skills auto-detected and grouped by category.* |
| 5 | `screenshot_gaps.png` | Skill Gaps tab | *Gap impact computed from 3,875 real postings — never invented percentages.* |
| 6 | `screenshot_careers.png` | Career Paths tab with evidence + "To strengthen" pills | *Role fit with receipts: what you have, and what would raise the score.* |
| 7 | `screenshot_jobs.png` | Jobs tab, top card expanded showing score breakdown bars | *1,245 gate-passing jobs ranked and explained: strong matches, missing skills, sub-scores.* |
| 8 | `screenshot_tailor.png` | Terminal: `resume_tailor --rank 1` output showing the validator's `[FLAGGED]` fallback | *The hallucination validator catching Mistral inventing Databricks + LangChain — and falling back to the truth.* |
| 9 | `screenshot_cover_letter.png` | Generated cover letter .docx open in Word | *One command from ranked job to reviewed, truthful cover letter.* |
| 10 | `demo.gif` | 20–30 s: upload → review → dashboard tabs → jobs → apply link | *(README hero GIF — record with ScreenToGif/Kap, keep under 10 MB.)* |

Also needed: `assets/banner.png` (1280×320). Simple approach: project name + tagline on a dark gradient with a subtle pipeline graphic; Figma/Canva, export 2×.

Tip: capture with the real test profile loaded so numbers match RESULTS.md (91/100, 3,875 jobs, etc.) — consistency between screenshots and documented results reads as rigor.
