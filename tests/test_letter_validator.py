"""The cover-letter validator (backend/resume_engine/letter_validator.py) on
the labelled fixture in tests/fixtures/letter_validator/: zero flags on the
three real Phase 5 letters, every synthetic fabrication still flagged, and
the old (resume-bullet) rules' output pinned so bullet validation provably
didn't change. Pure: no database, no LLM. Run with -s for the table."""
import json
from pathlib import Path

import pytest

from backend.resume_engine import cover_letter
from backend.resume_engine.letter_validator import validate_letter_paragraph
from backend.resume_engine.validate_tailored import validate_bullet

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "letter_validator" / "cases.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
PROFILE = DATA["profile"]
CASES, MISSES = DATA["cases"], DATA["known_misses"]


def _flags(case, letter_rules):
    review = {r["paragraph"]: r["flags"] for r in cover_letter.letter_review(
        case["paragraphs"], PROFILE, case["job"], letter_rules=letter_rules)}
    return [sorted(review.get(i, [])) for i in range(1, len(case["paragraphs"]) + 1)]


def _scores(cases, letter_rules):
    tp = fp = fn = 0
    for case in cases:
        for got, want in zip(_flags(case, letter_rules), case["expected"]):
            tp += len(set(got) & set(want))
            fp += len(set(got) - set(want))
            fn += len(set(want) - set(got))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {"tp": tp, "fp": fp, "fn": fn, "precision": round(precision, 3),
            "recall": round(recall, 3)}


def test_the_fixture_has_the_three_real_letters_and_the_required_fabrications():
    real = [c for c in CASES if c["kind"] == "real"]
    assert [c["id"] for c in real] == ["real-6", "real-7", "real-8"]
    assert sum(len(c["paragraphs"]) for c in real) == 12
    expected = {f for c in CASES if c["kind"] == "synthetic" for p in c["expected"] for f in p}
    assert {"Kubernetes", "40%", "Google", "Spark-based"} <= expected
    for c in CASES + MISSES:
        assert len(c["expected"]) == len(c["paragraphs"]) == len(c["bullet_rules_flags"])


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "real"],
                         ids=lambda c: c["id"])
def test_the_real_letters_raise_no_flags(case):
    assert _flags(case, letter_rules=True) == [[] for _ in case["paragraphs"]]


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "synthetic"],
                         ids=lambda c: c["id"])
def test_every_synthetic_fabrication_is_still_flagged(case):
    assert _flags(case, letter_rules=True) == case["expected"]


@pytest.mark.parametrize("case", MISSES, ids=lambda c: c["id"])
def test_the_known_misses_are_what_the_docs_say(case):
    # The sentence-start rule's cost: flagged by the old rules, not by these.
    assert _flags(case, letter_rules=False) == case["expected"]
    assert _flags(case, letter_rules=True) == [[] for _ in case["paragraphs"]]


@pytest.mark.parametrize("case", CASES + MISSES, ids=lambda c: c["id"])
def test_the_bullet_rules_output_is_unchanged(case):
    """letter_rules=False is the resume-bullet validator (validate_bullet),
    which the v1 UI and CLI keep using: its output on every paragraph is
    the one captured before the letter rules existed."""
    assert _flags(case, letter_rules=False) == case["bullet_rules_flags"]


def test_precision_and_recall_before_and_after():
    before, after = _scores(CASES, False), _scores(CASES, True)
    print(f"\n[letter-validator] before (bullet rules): {before}")
    print(f"[letter-validator] after  (letter rules): {after}")
    print(f"[letter-validator] with the known misses, after: {_scores(CASES + MISSES, True)}")
    assert before == {"tp": 5, "fp": 19, "fn": 0, "precision": 0.208, "recall": 1.0}
    assert after == {"tp": 5, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0}
    assert _scores(CASES + MISSES, True) == {"tp": 5, "fp": 0, "fn": 2,
                                             "precision": 1.0, "recall": 0.714}


# ---- the two rules, one at a time ------------------------------------------------

EVIDENCE = "Built a RAG pipeline with FAISS and Sentence Transformers for clinical Q&A."
SKILLS = ["RAG", "FAISS", "Sentence Transformers"]


def _terms(text):
    return validate_letter_paragraph(text, EVIDENCE, SKILLS)["flagged_terms"]


@pytest.mark.parametrize("text", [
    "My work used FAISS.",
    "This project used RAG. Thank you for reading!",
    "Furthermore, I used FAISS. Additionally, I used RAG? Lastly, thanks.",
    "\"This\" is how I used FAISS.",
    "(This was RAG.)",
])
def test_sentence_starters_are_not_flagged(text):
    assert _terms(text) == []
    assert validate_bullet(text, EVIDENCE, SKILLS)["flagged_terms"] != []   # the old rules


@pytest.mark.parametrize("text, flagged", [
    ("I deployed FAISS on Kubernetes.", ["Kubernetes"]),         # mid-sentence: checked
    ("I used RAG. Then I used Kubernetes.", ["Kubernetes"]),
    ("Tools I know, e.g. Kubernetes, are listed.", ["Kubernetes"]),  # e.g. ends no sentence
    ("I used tools, i.e. Kubernetes.", ["Kubernetes"]),
    ("RAGX powers it.", ["RAGX"]),                                # all-caps at the start
    ("PyTorch powers it.", ["PyTorch"]),                          # inner capitals
    ("GPT4 powers it.", ["GPT4"]),                                # digits
])
def test_tech_like_words_are_still_flagged(text, flagged):
    assert _terms(text) == flagged


def test_a_sentence_start_word_in_the_skill_taxonomy_is_still_checked():
    # "Huggingface" is in skill_taxonomy.SKILL_ALIASES, not in this evidence.
    assert _terms("Huggingface hosts the model.") == ["Huggingface"]
    assert _terms("Hosted on Huggingface.") == ["Huggingface"]


@pytest.mark.parametrize("text, flagged", [
    ("I built cutting-edge, high-stakes, decision-making systems with RAG.", []),
    ("I built Spark-based pipelines.", ["Spark-based"]),
    ("I built RAG-based pipelines and FAISS-backed search.", []),
    ("I built AWS-hosted search.", ["AWS-hosted"]),
    ("I used Retrieval-Augmented search.", ["Retrieval-Augmented"]),   # neither part listed
    ("I used sentence-transformers.", []),                             # whole token in aliases
])
def test_hyphenated_tokens_are_checked_part_by_part(text, flagged):
    assert _terms(text) == flagged


def test_numbers_are_checked_exactly_as_for_bullets():
    text = "My pipeline cut latency 40% and served 3x more users."
    assert validate_letter_paragraph(text, EVIDENCE, SKILLS)["flagged_numbers"] == \
        validate_bullet(text, EVIDENCE, SKILLS)["flagged_numbers"] == ["3x", "40%"]


def test_the_result_has_the_bullet_validators_shape():
    r = validate_letter_paragraph("This is fine.", EVIDENCE, SKILLS)
    assert r == {"ok": True, "flagged_terms": [], "flagged_numbers": []}


def test_the_default_letter_review_still_uses_the_bullet_rules():
    # The v1 UI (generate_for_job) and CLI keep their golden-pinned output.
    job = {"title": "AI Engineer", "company": "acme"}
    assert cover_letter.letter_review(["This works."], PROFILE, job) == [
        {"paragraph": 1, "flags": ["This"]}]
    assert cover_letter.letter_review(["This works."], PROFILE, job, letter_rules=True) == []


def test_agent_edits_use_the_letter_rules():
    from backend.agent.edits import apply_edits
    state = {"profile": PROFILE, "job": {"title": "AI Engineer", "company": "acme"},
             "paragraphs": ["One."], "resume_content": {"projects": []},
             "resume_report": [], "letter_review": []}
    clean = apply_edits(state, {"cover_letter_paragraphs": [
        "This cutting-edge RAG work is my strongest evidence."]})
    assert clean.flags == [] and clean.letter_review == []
    flagged = apply_edits(state, {"cover_letter_paragraphs": [
        "This RAG work ran on Kubernetes."]})
    assert flagged.flags == [{"document": "cover_letter", "paragraph": 1,
                              "flags": ["Kubernetes"]}]
