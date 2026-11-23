"""The cover-letter validator (backend/resume_engine/letter_validator.py) on
the labelled fixture in tests/fixtures/letter_validator/: zero flags on the
three real Phase 5 letters, every synthetic fabrication flagged (including
names that start a sentence), no flag on the target company or a name the
profile has, and the resume-bullet validator's output on the same text
pinned, so bullet validation provably didn't change. Pure: no database,
no LLM. Run with -s for the table."""
import json
import re
from pathlib import Path

import pytest

from backend.resume_engine import cover_letter, letter_validator
from backend.resume_engine.letter_validator import validate_letter_paragraph
from backend.resume_engine.validate_tailored import validate_bullet

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "letter_validator" / "cases.json"
DATA = json.loads(FIXTURE.read_text(encoding="utf-8"))
PROFILE = DATA["profile"]
CASES, MISSES = DATA["cases"], DATA["known_misses"]


@pytest.fixture(autouse=True)
def _fixture_corpus(monkeypatch):
    """Rule 3's corpus companies are the fixture's own list."""
    monkeypatch.setattr(letter_validator, "corpus_companies",
                        lambda: frozenset(DATA["corpus_companies"]))


def _flags(case):
    review = {r["paragraph"]: r["flags"] for r in cover_letter.letter_review(
        case["paragraphs"], PROFILE, case["job"])}
    return [sorted(review.get(i, [])) for i in range(1, len(case["paragraphs"]) + 1)]


def _bullet_flags(case):
    """The resume-bullet validator on each paragraph, filtered the way a
    letter review filters (company and title words are not flags)."""
    evidence = cover_letter._profile_evidence_text(PROFILE)
    skills = [s["skill"] for s in PROFILE["skills"]]
    job = case["job"]
    expected = {(job.get("company") or "").lower(),
                *(w.lower() for w in re.findall(r"[A-Za-z]+", job.get("title") or ""))}
    out = []
    for paragraph in case["paragraphs"]:
        r = validate_bullet(paragraph, evidence, skills)
        out.append(sorted([t for t in r["flagged_terms"] if t.lower() not in expected]
                          + r["flagged_numbers"]))
    return out


def _scores(cases, flags):
    tp = fp = fn = 0
    for case in cases:
        for got, want in zip(flags(case), case["expected"]):
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
    assert {"Kubernetes", "40%", "Google", "Spark-based", "Kafka-backed", "Microsoft",
            "Tata Consultancy Services", "Groww", "Hugging Face"} <= expected
    starts = [c["paragraphs"][0] for c in CASES if c["id"] in (
        "tp-company-at-sentence-start", "tp-company-after-at")]
    assert starts[0].startswith("Google hired me") and starts[1].startswith("At Microsoft, I")
    for c in CASES + MISSES:
        assert len(c["expected"]) == len(c["paragraphs"]) == len(c["bullet_rules_flags"])


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "real"],
                         ids=lambda c: c["id"])
def test_the_real_letters_raise_no_flags(case):
    assert _flags(case) == [[] for _ in case["paragraphs"]]


@pytest.mark.parametrize("case", [c for c in CASES if c["kind"] == "synthetic"],
                         ids=lambda c: c["id"])
def test_every_synthetic_case_is_flagged_exactly_as_labelled(case):
    assert _flags(case) == case["expected"]


@pytest.mark.parametrize("case", MISSES, ids=lambda c: c["id"])
def test_the_known_misses_are_what_the_docs_say(case):
    # A name in none of rule 3's lists that starts a sentence: the old rules
    # flagged it (with every other sentence starter); these don't.
    assert _bullet_flags(case) == case["expected"]
    assert _flags(case) == [[] for _ in case["paragraphs"]]


@pytest.mark.parametrize("case", CASES + MISSES, ids=lambda c: c["id"])
def test_the_bullet_validators_output_is_unchanged(case):
    """validate_bullet (resume bullets) on every paragraph gives exactly the
    output captured before the letter rules existed."""
    assert _bullet_flags(case) == case["bullet_rules_flags"]


def test_precision_and_recall_before_and_after():
    before, after = _scores(CASES, _bullet_flags), _scores(CASES, _flags)
    with_misses = _scores(CASES + MISSES, _flags)
    print(f"\n[letter-validator] before (bullet rules): {before}")
    print(f"[letter-validator] after  (letter rules): {after}")
    print(f"[letter-validator] with the known misses, after: {with_misses}")
    assert before == {"tp": 9, "fp": 24, "fn": 2, "precision": 0.273, "recall": 0.818}
    assert after == {"tp": 11, "fp": 0, "fn": 0, "precision": 1.0, "recall": 1.0}
    assert with_misses == {"tp": 11, "fp": 0, "fn": 1, "precision": 1.0, "recall": 0.917}


# ---- the rules, one at a time ------------------------------------------------

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


def test_every_letter_review_uses_the_letter_rules():
    # One letter validator: the UI (generate_for_job), the CLI and the agent.
    job = {"title": "AI Engineer", "company": "acme"}
    assert cover_letter.letter_review(["This works."], PROFILE, job) == []
    assert cover_letter.letter_review(["This works at Google."], PROFILE, job) == [
        {"paragraph": 1, "flags": ["Google"]}]
    assert [r["flagged_terms"] for r in cover_letter.validate_letter(
        ["Thank you."], PROFILE, job)] == [[]]


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


@pytest.mark.parametrize("text, flagged", [
    ("Google hired me as an intern.", ["Google"]),                # curated list
    ("Kubernetes runs my RAG service.", ["Kubernetes"]),
    ("Docker-based FAISS search.", ["Docker-based"]),             # a hyphenated head
    ("RAG-Kubernetes pipelines run here.", ["RAG-Kubernetes"]),   # every part checked
    ("Tata Consultancy Services, Google and I.", ["Google", "Tata Consultancy Services"]),
    ("I worked at Hugging Face.", ["Hugging Face"]),              # one term, mid-sentence
    ("Acme hired me.", ["Acme"]),                                 # a corpus company
    ("Sentence Transformers power it.", []),                      # the profile has it
    ("FAISS and RAG power it.", []),
    ("Hello. Google? No. Amazon! Kubernetes.", ["Amazon", "Google", "Kubernetes"]),
])
def test_rule_3_known_names_that_start_a_sentence(text, flagged, monkeypatch):
    monkeypatch.setattr(letter_validator, "corpus_companies", lambda: frozenset({"acme"}))
    assert _terms(text) == flagged


def test_rule_3_never_flags_the_target_company():
    for company in ("Google", "google", "Tata Consultancy Services", "OpenAI"):
        text = f"{company} is where I want to apply my RAG work."
        assert validate_letter_paragraph(text, EVIDENCE, SKILLS,
                                         target_company=company)["flagged_terms"] == []
    assert validate_letter_paragraph("Google hired me.", EVIDENCE, SKILLS,
                                     target_company="microsoft")["flagged_terms"] == ["Google"]


def test_rule_3_reads_the_corpus_companies_from_the_json_corpus(tmp_path, monkeypatch):
    from backend.database import corpus
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps([{"title": "AI Engineer", "company": "Zyphra"}]),
                    encoding="utf-8")
    monkeypatch.setattr(corpus, "JOBS_FILE", jobs)
    monkeypatch.setattr(letter_validator, "corpus_companies",
                        letter_validator._corpus_companies)
    monkeypatch.setattr(letter_validator, "_corpus_cache", None)
    assert letter_validator.corpus_companies() == frozenset({"Zyphra"})
    assert _terms("Zyphra hired me.") == ["Zyphra"]
    jobs.write_text(json.dumps([{"title": "AI Engineer", "company": "Other"}]),
                    encoding="utf-8")
    assert letter_validator.corpus_companies() == frozenset({"Zyphra"})   # cached
    monkeypatch.setattr(letter_validator, "_corpus_cache", None)
    assert letter_validator.corpus_companies() == frozenset({"Other"})


def test_rule_3_without_a_corpus_still_has_its_curated_lists(monkeypatch):
    from backend.database import corpus

    def broken(*a, **kw):
        raise OSError("no corpus")
    monkeypatch.setattr(corpus, "load_corpus_snapshot", broken)
    monkeypatch.setattr(letter_validator, "corpus_companies",
                        letter_validator._corpus_companies)
    monkeypatch.setattr(letter_validator, "_corpus_cache", None)
    assert letter_validator.corpus_companies() == frozenset()
    assert letter_validator.entity_key("Hugging Face") in letter_validator.known_entities()
    assert _terms("Microsoft hired me.") == ["Microsoft"]
