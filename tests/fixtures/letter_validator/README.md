# Cover-letter validator fixture

`cases.json` is a labelled set for the cover-letter truthfulness check
(`backend/resume_engine/letter_validator.py`, used by the agent through
`cover_letter.letter_review(..., letter_rules=True)`).
`tests/test_letter_validator.py` runs it.

- **`profile`**: the evidence the letters are checked against: the real
  candidate profile's skills and projects. Name, contact details and
  education were left out; no paragraph here relies on them.
- **`cases`**, each with `job` (title and company: a letter may name them),
  `paragraphs`, and `expected`: per paragraph, the claims a correct
  validator must flag.
  - `kind: "real"`: the three cover letters the Phase 5 real run drafted
    (Ollama qwen2.5:1.5b q4_K_M). They make no unverified claim, so
    `expected` is empty for every paragraph. Under the old rules they
    raised 19 flags, all false positives (`My`, `This`, `Thank`,
    `cutting-edge`, `decision-making`, ...), which made all three
    `needs_review`.
  - `kind: "synthetic"`: one fabricated claim per paragraph, written on
    top of the real projects: an unlisted tool (`Kubernetes`), the same tool
    right after a sentence-starting `This`, an invented metric
    (`cut latency 40%`), an unlisted company (`Google`) and a hyphenated
    unlisted tool (`Spark-based`). Every one must stay flagged.
- **`known_misses`**: the documented cost of the sentence-start rule. An
  unlisted company or tool that *starts* a sentence (`Google hired me ...`,
  `Kafka-backed streaming ...`) is no longer flagged, unless it is in the
  skill taxonomy or tech-like by another rule. The same claim mid-sentence
  is (see the synthetic cases). The test pins this, so changing it is a
  visible decision.
- **`bullet_rules_flags`**: what the old rules (the resume-bullet
  validator, `letter_rules=False`, still used by the v1 UI and CLI) report
  for each paragraph, captured before the letter rules existed. The test
  pins it, which shows that bullet validation is unchanged.

Precision and recall, counted per flag over `cases`:

| rules | true flags | false flags | missed | precision | recall |
|---|---|---|---|---|---|
| before (bullet rules) | 5 | 19 | 0 | 0.208 | 1.000 |
| after (letter rules) | 5 | 0 | 0 | 1.000 | 1.000 |

With `known_misses` added: after, 5 / 0 / 2, precision 1.000, recall 0.714
(before, 7 / 19 / 0: 0.269 / 1.000).
