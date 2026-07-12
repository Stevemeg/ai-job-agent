"""
validate_tailored.py -- Post-processing truthfulness check for LLM-tailored
resume bullets.

WHY THIS EXISTS: Mistral 7B, despite explicit negative examples in the
tailoring prompt, still occasionally injects tools/platforms (Databricks,
LangChain, DSPy) or metrics that don't exist in the original resume. A
prompt can only lower the probability of hallucination; this module makes
fabrication *detectable* deterministically, with no LLM in the loop.

DESIGN: a tailored bullet may only contain
  1. Tech-like terms (acronyms, CamelCase, capitalized proper nouns) that
     appear in the ORIGINAL bullet text, the candidate's skill list, or a
     known alias of either (reuses skill_taxonomy.SKILL_ALIASES).
  2. Numbers/metrics that appear in the original bullet text.
Anything else is flagged. The caller decides what to do with flags --
resume_tailor.py falls back to the original bullets for any project that
fails validation.

This is intentionally conservative-ish rather than perfect: generic English
words are whitelisted so ordinary rephrasing doesn't false-positive, and
flagged terms are reported (not silently dropped) so a human reviews them.
"""
import json
import re
import sys

try:
    from ..matching_engine.skill_taxonomy import SKILL_ALIASES
except ImportError:          # allow standalone use without package context
    SKILL_ALIASES = {}

# Ordinary English words that legitimately appear capitalized (sentence
# starts, action verbs, common resume vocabulary). Lowercase for comparison.
_COMMON_WORDS = {
    "a", "an", "the", "and", "or", "for", "with", "using", "via", "on", "in",
    "of", "to", "by", "from", "as", "at", "into", "across", "over", "per",
    "built", "designed", "developed", "engineered", "architected", "created",
    "implemented", "deployed", "delivered", "achieved", "improved", "reduced",
    "increased", "optimized", "led", "spearheaded", "integrated", "evaluated",
    "generated", "produced", "trained", "tested", "validated", "automated",
    "streamlined", "enhanced", "accelerated", "enabled", "ensured",
    "leveraged", "utilized", "applied", "constructed", "established",
    "pipeline", "pipelines", "model", "models", "system", "systems", "data",
    "training", "inference", "accuracy", "latency", "retrieval", "search",
    "semantic", "attention", "embedding", "embeddings", "evaluation",
    "metrics", "quality", "production", "grade", "end", "custom", "local",
    "private", "offline", "medical", "clinical", "synthetic", "images",
    "image", "imaging", "document", "documents", "chunks", "sources",
    "responses", "hallucinations", "compliance", "privacy", "scarcity",
    "stability", "realism", "batches", "checkpointing", "interpretability",
    "heatmap", "overlays", "spatial", "failure", "analysis", "logs", "blind",
    "spots", "reasoning", "visual", "language", "text", "vision", "fusing",
    "joint", "structured", "answer", "types", "yes", "no", "number", "open",
    "ended", "subset", "top", "mechanism", "layer", "module", "cli",
    "eliminating", "reliance", "subjective", "inspection", "expanding",
    "datasets", "dataset", "settings", "grounding", "strictly", "verified",
    "indexing", "sub", "second", "zero", "external", "api", "dependency",
    "full", "fully", "addressing", "critical", "consistent", "all",
    "ensuring", "dependencies",
}

# Term looks "tech-like" if: ALL-CAPS acronym (2+ chars), internal capitals
# (CamelCase / mixed), digit+letter mix (e.g. "GPT-4"), or dotted/hyphenated
# tech tokens (e.g. "scikit-learn", "node.js").
_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.#/-]*[A-Za-z0-9+#]|[A-Za-z]")
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*[%x×+]?")


def _norm(s):
    return " ".join(s.lower().split())


def _stem(word):
    """Light suffix stripping so legitimate inflections don't false-positive:
    'Indexed' must validate against an original that says 'indexing'.
    Deliberately minimal -- real stemmers (Porter) over-merge and would let
    fabrications through; this only strips one common verb/plural suffix."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: len(word) - len(suffix)]
    return word


def _alias_forms(term_lower):
    """All lowercase surface forms that count as the same skill."""
    forms = {term_lower}
    forms |= SKILL_ALIASES.get(term_lower, set())
    # reverse direction: if term is an alias of some canonical skill
    for canonical, aliases in SKILL_ALIASES.items():
        if term_lower in aliases:
            forms.add(canonical)
            forms |= aliases
    return forms


def _is_tech_like(token):
    if len(token) >= 2 and token.isupper():
        return True                              # RAG, LLM, FID, SSIM
    if any(c.isupper() for c in token[1:]):
        return True                              # PyTorch, HuggingFace
    if any(c.isdigit() for c in token) and any(c.isalpha() for c in token):
        return True                              # GPT-4, VQA2
    if ("-" in token or "." in token) and token.lower() not in _COMMON_WORDS:
        return True                              # scikit-learn, node.js
    if token[:1].isupper() and token.lower() not in _COMMON_WORDS:
        return True                              # Databricks, Ollama
    return False


def _extract_tech_terms(text):
    return {t for t in _TERM_RE.findall(text) if _is_tech_like(t)}


def _extract_numbers(text):
    return {n.replace(",", "").replace(" ", "") for n in _NUMBER_RE.findall(text)}


def validate_bullet(bullet, source_text, allowed_skills=()):
    """Check one tailored bullet against the original source text.

    Returns dict: {"ok": bool, "flagged_terms": [...], "flagged_numbers": [...]}
    """
    source_lower = _norm(source_text)
    allowed_lower = {_norm(s) for s in allowed_skills}
    source_stems = {_stem(w) for w in re.findall(r"[a-z0-9+#.-]+", source_lower)}

    flagged_terms = []
    for term in _extract_tech_terms(bullet):
        term_l = _norm(term)
        forms = _alias_forms(term_l)
        in_source = any(f in source_lower for f in forms)
        in_skills = any(f in allowed_lower for f in forms)
        # Inflection tolerance for single words: 'Indexed' vs 'indexing'
        in_stems = " " not in term_l and _stem(term_l) in source_stems
        if not in_source and not in_skills and not in_stems:
            flagged_terms.append(term)

    source_numbers = _extract_numbers(source_text)
    # compare on the numeric part only ("33%" matches "~33 %")
    source_numeric = {re.sub(r"[^\d.]", "", n) for n in source_numbers}
    flagged_numbers = []
    for num in _extract_numbers(bullet):
        numeric = re.sub(r"[^\d.]", "", num)
        if numeric and numeric not in source_numeric:
            flagged_numbers.append(num)

    return {
        "ok": not flagged_terms and not flagged_numbers,
        "flagged_terms": sorted(set(flagged_terms)),
        "flagged_numbers": sorted(set(flagged_numbers)),
    }


def validate_project(tailored_bullets, original_description, allowed_skills=()):
    """Validate all tailored bullets for one project.

    Returns dict: {"ok": bool, "bullets": [per-bullet results]}
    """
    results = [validate_bullet(b, original_description, allowed_skills)
               for b in tailored_bullets]
    return {"ok": all(r["ok"] for r in results), "bullets": results}


def print_report(project_title, tailored_bullets, result):
    """Human-readable summary of a validation result."""
    if result["ok"]:
        print(f"  [OK] {project_title}: all bullets verified against original")
        return
    print(f"  [FLAGGED] {project_title}:")
    for bullet, r in zip(tailored_bullets, result["bullets"]):
        if r["ok"]:
            continue
        print(f"    Bullet: {bullet[:80]}...")
        if r["flagged_terms"]:
            print(f"      Unverified terms: {r['flagged_terms']}")
        if r["flagged_numbers"]:
            print(f"      Unverified numbers: {r['flagged_numbers']}")


if __name__ == "__main__":
    # Standalone smoke test with known-hallucination examples.
    original = (" • Designed and deployed a Retrieval-Augmented Generation "
                "pipeline for clinical Q&A. • Implemented semantic search using "
                "Sentence Transformers + FAISS, indexing 2,500+ document chunks. "
                "• Integrated a local LLM (Ollama) for offline inference.")
    skills = ["RAG", "LLM", "FAISS", "Sentence Transformers", "Ollama"]

    good = "Deployed a RAG pipeline for clinical Q&A using FAISS, indexing 2,500+ chunks."
    bad = "Built RAG pipeline on Databricks using LangChain, serving 10,000 users."

    r_good = validate_bullet(good, original, skills)
    r_bad = validate_bullet(bad, original, skills)
    print("GOOD bullet ->", json.dumps(r_good))
    print("BAD bullet  ->", json.dumps(r_bad))
    assert r_good["ok"], "good bullet should pass"
    assert not r_bad["ok"], "bad bullet should be flagged"
    assert "Databricks" in r_bad["flagged_terms"]
    assert "LangChain" in r_bad["flagged_terms"]
    assert any("10000" in n for n in r_bad["flagged_numbers"])
    print("Smoke test passed.")
    sys.exit(0)
