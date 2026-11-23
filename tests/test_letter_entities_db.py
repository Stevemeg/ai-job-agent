"""The letter validator's rule 3 with a real Postgres: the companies of the
job corpus come from canonical_jobs (collected rows only)."""
import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import text                       # noqa: E402

from backend.database import corpus               # noqa: E402
from backend.resume_engine import letter_validator  # noqa: E402

pytestmark = pytest.mark.db

EVIDENCE = "Built a RAG pipeline with FAISS."


def test_corpus_companies_come_from_canonical_jobs(pg, monkeypatch):
    corpus.upsert_jobs([{"title": "AI Engineer", "company": "Zyphra", "location": "Pune",
                         "tags": [], "clean_description": "RAG.", "source": "ats"}])
    with pg.begin() as conn:                   # a stub row (never collected) is not corpus
        conn.execute(text("INSERT INTO canonical_jobs (company, title, normalized_title) "
                          "VALUES ('Stubco', 'X', 'x')"))
    monkeypatch.setattr(letter_validator, "corpus_companies",
                        letter_validator._corpus_companies)
    monkeypatch.setattr(letter_validator, "_corpus_cache", None)
    assert letter_validator.corpus_companies() == frozenset({"Zyphra"})
    flagged = letter_validator.validate_letter_paragraph(
        "Zyphra hired me. Stubco did too.", EVIDENCE, ["RAG"])["flagged_terms"]
    assert flagged == ["Zyphra"]
    assert letter_validator.validate_letter_paragraph(
        "Zyphra is where I want to work.", EVIDENCE, ["RAG"],
        target_company="zyphra")["flagged_terms"] == []
