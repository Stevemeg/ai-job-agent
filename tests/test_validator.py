from backend.resume_engine.validate_tailored import (validate_bullet,
                                                     validate_project)

ORIGINAL = (" • Designed and deployed a Retrieval-Augmented Generation "
            "pipeline for clinical Q&A. • Implemented semantic search using "
            "Sentence Transformers + FAISS, indexing 2,500+ document chunks. "
            "• Integrated a local LLM (Ollama) for offline inference.")
SKILLS = ["RAG", "LLM", "FAISS", "Sentence Transformers", "Ollama"]


def test_truthful_rewrite_passes():
    r = validate_bullet(
        "Deployed a RAG pipeline for clinical Q&A with FAISS, indexing "
        "2,500+ chunks.", ORIGINAL, SKILLS)
    assert r["ok"], r


def test_fabricated_tools_flagged():
    r = validate_bullet(
        "Built RAG pipeline on Databricks using LangChain and DSPy.",
        ORIGINAL, SKILLS)
    assert not r["ok"]
    assert {"Databricks", "LangChain", "DSPy"} <= set(r["flagged_terms"])


def test_fabricated_metrics_flagged():
    r = validate_bullet("Served 50,000 users with 99.9% uptime.",
                        ORIGINAL, SKILLS)
    assert not r["ok"]
    numbers = " ".join(r["flagged_numbers"])
    assert "50000" in numbers and "99.9" in numbers


def test_original_numbers_pass_with_formatting_changes():
    r = validate_bullet("Indexed 2500+ chunks efficiently.", ORIGINAL, SKILLS)
    assert r["ok"], r        # "2,500+" vs "2500+" must match


def test_alias_awareness():
    r = validate_bullet("Deployed a RAG system.",
                        "Built a Retrieval-Augmented Generation pipeline.", [])
    assert r["ok"], r


def test_project_level_fallback_signal():
    bullets = ["Deployed a RAG pipeline for clinical Q&A.",
               "Migrated everything to Kubernetes on AWS."]
    result = validate_project(bullets, ORIGINAL, SKILLS)
    assert not result["ok"]
    assert result["bullets"][0]["ok"] and not result["bullets"][1]["ok"]
