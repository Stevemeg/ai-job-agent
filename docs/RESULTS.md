# Results

All numbers are real outputs from the current corpus (3,875 jobs) and the developer's test profile. Nothing here is estimated.

## Job Collection & Gating

| Metric | Value |
|---|---|
| Jobs collected (Greenhouse + Ashby, deduplicated) | 3,875 |
| Passing all hard gates | 1,245 (32.1%) |
| Removed: required years > 3 | 54.3% |
| Removed: title keywords | 13.1% |
| Removed: security clearance | 0.4% |
| Removed: no sponsorship | 0.1% |

## Ranking Quality (test profile)

| Metric | Value |
|---|---|
| Jobs with skill_overlap > 0 | 394 / 1,245 |
| Top match | AI Engineer – FDE @ Databricks, score 40.63 |
| Top-match breakdown | skill 0.20 · semantic 0.61 · role 1.00 · seniority 1.00 |
| Category-level (Mode B) matches in corpus | 1 / 1,245 — "Researcher, Trustworthy AI" @ OpenAI via "adversarial training" |
| Observed skill_overlap range | 0.09–0.20 (JDs use broad language; semantic sub-score carries that signal) |

## Resume Parsing & Confidence (test profile)

| Section | Confidence | Notes |
|---|---|---|
| Overall | 98% | |
| Contact | 100% | All five fields extracted |
| Skills | 100% | 22 skills |
| Projects | 90% | Correctly flagged a real boundary bug: project 1's description bleeding into project 2's title |
| Education | 100% | |

The Projects flag is the headline result: the confidence system detected the parser's one known real defect without being told about it.

## Resume Health (test profile)

| Dimension | Score / Max |
|---|---|
| Contact & Links | 15 / 15 |
| Skills | 20 / 20 |
| Projects | 20 / 20 |
| Quantified Impact | **6 / 15** — 4/10 bullets contain metrics |
| Education | 10 / 10 |
| ATS Readiness | 20 / 20 |
| **Overall** | **91 / 100 (A)** |

## Career Recommendation (test profile)

| Role | Fit | Evidence (sample) |
|---|---|---|
| Computer Vision Engineer | 88% | opencv, clip, cnn, image — 0 missing core signals |
| NLP Engineer | 79% | transformers, distilbert, sentence transformers, semantic search |
| AI Engineer | 77% | llm, rag, prompt engineering, ollama |
| LLM / GenAI Engineer | 74% | llm, rag, generative, gan |
| ML Engineer | 66% | pytorch, scikit-learn, cnn |
| Data Scientist | 59% | pandas, numpy, scikit-learn |

## Skill Gap Analysis (test profile)

Target-role subset was 17 jobs — below the 30-job honesty threshold, so overall demand is headlined (the guard working as designed):

| Missing skill | Overall demand | Priority |
|---|---|---|
| Spark | 18.6% (719 postings) | Medium |
| SQL | 16.3% (630) | Medium |
| AWS | 13.2% (512) | Medium |
| GCP | 9.6% (371) | Low |
| Azure | 8.3% (321) | Low |
| Kubernetes | 7.9% | Low |

Methodology note: the naive `\bgo\b` regex measured 26.1% "demand" for Go; the corrected pattern measures 0.9%. Documented as a case study in regex discipline for corpus statistics.

## Hallucination Validator

| Test | Result |
|---|---|
| Adversarial bullets (fabricated Databricks, LangChain, DSPy, AWS + invented "50,000 users", "99.9% uptime") | All flagged ✅ |
| Truthful rephrasings of real bullets | 0 false positives ✅ |
| Alias awareness ("RAG" vs original's "Retrieval-Augmented Generation") | Passes ✅ |
| Live production behavior | Caught Mistral adding Databricks/LangChain/DSPy; fell back to original bullets |

## Evaluation Metrics That Should Be Collected Next

The honest gap in this table: everything above measures the *system's internals*; nothing yet measures *outcomes*. As real usage accumulates:

1. **Ranking:** precision@10 against user relevance judgments; NDCG once outcome events exist
2. **Funnel:** application → response rate, → interview rate, segmented by match-score band (does a 40-score job outperform a 20-score job in practice?)
3. **Parser:** field-level extraction accuracy on a labeled set of ~50 diverse resumes (the current n=1 profile proves nothing about generalization)
4. **Validator:** false-positive/false-negative rates on a labeled set of tailored bullets
5. **Tailoring:** ATS parse-rate of generated .docx files through real ATS parsers (e.g. open-source resume parsers as proxies)
6. **Health score:** correlation between score and human recruiter ratings — the score is currently principled but uncalibrated
