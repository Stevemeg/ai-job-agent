# Resume Engine

Covers two subsystems: the **parser** (`backend/resume_parser/`) that turns a PDF into a structured profile, and the **tailoring engine** (`backend/resume_engine/`) that produces job-specific documents.

## 1. Resume Parser (`backend/resume_parser/`)

| Module | Responsibility |
|---|---|
| `api.py` | Single entry point: `parse_resume_pdf(path) -> dict`, `save_profile(profile)` (with `.bak` backup). Fixes the legacy relative-path bug; all external callers go through here. |
| `resume_parser.py` | PDF text extraction (PyMuPDF, block-sorted), link extraction, link classification (LinkedIn/GitHub). |
| `text_cleaner.py` | Normalizes raw extracted text. |
| `entity_extractor.py` | Name, email, phone. |
| `skill_extractor.py` | Word-boundary regex match against `datasets/skills_database.csv` (skill, category, domain). |
| `education_extractor.py` | Degree, institution, CGPA, years. |
| `project_extractor.py` | Project titles, durations, bullet descriptions. Historically the most error-prone extractor (empty titles, merged boundaries) — which is why parse confidence exists. |

**Output schema** (`data/candidate_profile.json`):

```json
{
  "name": "...", "email": "...", "phone": "...",
  "linkedin": "...", "github": "...",
  "skills":    [{"skill": "RAG", "category": "Generative AI", "domain": "AI"}],
  "education": [{"college": "...", "degree": "...", "cgpa": "...", "years": "..."}],
  "projects":  [{"title": "...", "duration": "...", "description": " • bullet • bullet"}]
}
```

### Parse Confidence (`backend/analysis/parse_confidence.py`)

The parser grades its own output so users know what to double-check:

- **Contact** — email regex validity, name plausibility, phone digit count
- **Skills** — count vs. expectation
- **Projects** — title validity, bullet count, duration presence, and a *cross-contamination check* (does project N's description bleed into project N+1's title?)
- **Education** — field completeness

Real result on the test profile: 98% overall, and the contamination check correctly flagged the one genuine boundary bug in the parsed profile.

## 2. Resume Tailoring (`backend/resume_engine/`)

| Module | Responsibility |
|---|---|
| `resume_tailor.py` | Loads a ranked job, extracts JD requirements via Ollama/Mistral, rewrites project bullets under strict truthfulness rules, reorders skills, emits content JSON. |
| `validate_tailored.py` | **Deterministic hallucination validator** (see below). |
| `cover_letter.py` | Same pipeline shape → 3 grounded paragraphs → `.txt` + `.docx`, validated per paragraph. |
| `generate_resume_docx.js` | Node/docx.js — ATS-safe layout: single column, no tables/text-boxes, standard headings, US Letter. |
| `generate_cover_letter_docx.js` | Matching cover letter layout. |

### The Hallucination Validator

Mistral 7B occasionally injects tools it "expects" to see (observed: Databricks, LangChain, DSPy) despite explicit negative examples in the prompt. The validator makes this detectable rather than hoping the prompt holds:

1. Extract *tech-like terms* from each tailored bullet (acronyms, CamelCase, capitalized non-common words, digit-letter mixes).
2. Each term must appear in the original bullet text, the candidate's skill list, or a known alias of either (reuses `skill_taxonomy.SKILL_ALIASES`, so "RAG" validates against "Retrieval-Augmented Generation").
3. Every number/metric in a tailored bullet must exist in the original.
4. Any project with a flagged bullet **falls back to its original bullets** — an untailored true resume beats a tailored fraudulent one.

Adversarial test results: caught 3 fabricated tools and 2 fabricated metrics; zero false positives on truthful rephrasings of the same content.

### Known Limitations

- Experience and certification extraction are **not implemented** (planned — see [ROADMAP.md](ROADMAP.md)).
- Skill extraction is dictionary-based; skills absent from `skills_database.csv` are invisible to the parser (mitigated by the Review & Edit step).
- The validator is conservative: an uncommon-but-legitimate capitalized word can be flagged. Flags are always shown, never silently applied.
