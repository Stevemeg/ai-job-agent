# Cover-letter validator fixture

`cases.json` is a labelled set for the cover-letter truthfulness check
(`backend/resume_engine/letter_validator.py`, used for every cover letter
through `cover_letter.letter_review`). `tests/test_letter_validator.py`
runs it.

- **`profile`**: the evidence the letters are checked against: the real
  candidate profile's skills and projects. Name, contact details and
  education were left out; no paragraph here relies on them.
- **`corpus_companies`**: the job-corpus company names rule 3 uses in the
  test (in production they come from `canonical_jobs`).
- **`cases`**, each with `job` (title and company: a letter may name them),
  `paragraphs`, and `expected`: per paragraph, the claims a correct
  validator must flag.
  - `kind: "real"`: the three cover letters the Phase 5 real run drafted
    (Ollama qwen2.5:1.5b q4_K_M). They make no unverified claim, so
    `expected` is empty for every paragraph. Under the old rules they
    raised 19 flags, all false positives (`My`, `This`, `Thank`,
    `cutting-edge`, `decision-making`, ...).
  - `kind: "synthetic"`, written on top of the real projects. Fabrications
    that must be flagged: an unlisted tool (`Kubernetes`, also right after a
    sentence-starting `This`), an invented metric (`cut latency 40%`), an
    unlisted company mid-sentence (`Google`, `At Microsoft, I ...`), a
    hyphenated unlisted tool (`Spark-based`), and names that START a
    sentence (rule 3): `Google hired me ...`, `Kafka-backed ...`,
    `Tata Consultancy Services ...`, `Hugging Face ...` and a corpus
    company (`Groww's ...`). And two that must NOT be flagged: the target
    company starting a sentence, and a known name the profile has
    (`Sentence Transformers ...`).
- **`known_misses`**: what is still missed, kept out of the scored set: a
  company in none of rule 3's lists (corpus, skill taxonomy, curated) that
  starts a sentence (`Zyphra hired me ...`). Mid-sentence it would be
  flagged. The test pins this, so changing it is a visible decision.
- **`bullet_rules_flags`**: what the resume-bullet validator
  (`validate_tailored.validate_bullet`, the rules letters used before
  Phase 6) reports for each paragraph, captured before the letter rules
  existed. The test pins it, which shows that bullet validation is
  unchanged.

Precision and recall, counted per flag over `cases`:

| rules | true flags | false flags | missed | precision | recall |
|---|---|---|---|---|---|
| before (bullet rules) | 9 | 24 | 2 | 0.273 | 0.818 |
| after (letter rules 1-3) | 11 | 0 | 0 | 1.000 | 1.000 |

With `known_misses` added: after, 11 / 0 / 1, precision 1.000, recall 0.917.
