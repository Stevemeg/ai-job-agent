"""
letter_validator.py -- the truthfulness check for cover-letter PARAGRAPHS
(Phase 6). Resume bullets keep validate_tailored.validate_bullet, which
this module does not change.

Why a letter needs its own rules: validate_bullet treats every capitalized
word that isn't in its small common-word list as a possible tool or
company. Resume bullets start with a verb from that list, but letters are
prose, so every sentence-starter ("My", "This", "Thank", "Furthermore")
and every hyphenated adjective ("cutting-edge", "decision-making") came
back flagged. All three real drafts in the Phase 5 run were needs_review
for nothing else.

Same contract as validate_bullet: {"ok", "flagged_terms",
"flagged_numbers"}, the same numbers check, and the same lookup (the
profile evidence, the candidate's skills, skill_taxonomy aliases,
inflections). Two rules differ, both for letters only:

1. A capitalized word at the START of a sentence (the paragraph's first
   word, or the first word after . ! or ? -- "e.g." and similar
   abbreviations don't end a sentence) is not tech-like on its
   capital alone. It still is when another rule makes it tech-like
   (ALL-CAPS, inner capitals, letters mixed with digits) or when it is in
   the skill taxonomy (skill_taxonomy.SKILL_ALIASES). Mid-sentence
   capitals are checked as before.
2. A hyphenated token is checked part by part. An all-lowercase
   alphabetic part ("edge", "based", "making") is never tech-like; a
   tech-like part ("Spark" in "Spark-based", "AI" in "AI-powered") must be
   in the evidence like any other term. The whole token is reported when a
   part fails, and a token found whole in the evidence passes.

(And "e.g" / "i.e", which the term pattern reads as dotted tokens, are
prose, not tools.)

Known trade-off of rule 1: an unlisted company or tool that STARTS a
sentence ("Google hired me as an intern.") is not flagged unless it is
also tech-like by rule 2's tests or in the taxonomy. The same claim
mid-sentence ("an internship at Google") is. The fixture in
tests/fixtures/letter_validator/ records it.
"""
from __future__ import annotations

import re

from .validate_tailored import (SKILL_ALIASES, _COMMON_WORDS, _TERM_RE, _alias_forms,
                                _extract_numbers, _norm, _stem)

# A period after one of these doesn't end a sentence ("e.g. Kubernetes").
# Not "etc." or "Inc.": they end sentences about as often as not, and the
# word after a real sentence end is the false positive this module removes.
_ABBREVIATIONS = frozenset({"e.g", "i.e", "vs", "cf", "approx", "mr", "mrs", "ms", "dr",
                            "prof"})
_OPENERS = "\"'([{“‘"
_TAXONOMY = frozenset(SKILL_ALIASES) | frozenset(a for forms in SKILL_ALIASES.values()
                                                for a in forms)
_LAST_WORD = re.compile(r"([A-Za-z][A-Za-z.]*)\.$")

__all__ = ["validate_letter_paragraph"]


def _at_sentence_start(text: str, start: int) -> bool:
    before = text[:start].rstrip().rstrip(_OPENERS).rstrip()
    if not before:
        return True
    if before[-1] in "!?":
        return True
    if before[-1] != ".":
        return False
    m = _LAST_WORD.search(before)
    return not (m and m.group(1).lower() in _ABBREVIATIONS)


def _in_taxonomy(term: str) -> bool:
    return _norm(term) in _TAXONOMY


def _tech_like(part: str, at_start: bool) -> bool:
    """validate_tailored._is_tech_like for one word of a letter, without its
    hyphen rule (hyphens are split by the caller) and with rule 1."""
    if len(part) >= 2 and part.isupper():
        return True                              # RAG, LLM, AI
    if any(c.isupper() for c in part[1:]):
        return True                              # PyTorch, MobileNetV2
    if any(c.isdigit() for c in part) and any(c.isalpha() for c in part):
        return True                              # VQA2, GPT4
    if "." in part and part.lower() in _ABBREVIATIONS:
        return False                             # e.g, i.e: prose, not a tool
    if "." in part and part.lower() not in _COMMON_WORDS:
        return True                              # node.js
    if part[:1].isupper() and part.lower() not in _COMMON_WORDS:
        return not at_start or _in_taxonomy(part)   # Databricks; not "This"
    return False


class _Evidence:
    def __init__(self, source_text: str, allowed_skills):
        self.source_lower = _norm(source_text)
        self.allowed_lower = {_norm(s) for s in allowed_skills}
        self.stems = {_stem(w) for w in re.findall(r"[a-z0-9+#.-]+", self.source_lower)}

    def verifies(self, term: str) -> bool:
        """validate_bullet's lookup, unchanged: evidence text, skills or a
        taxonomy alias of either, or a light inflection of one word."""
        term_l = _norm(term)
        forms = _alias_forms(term_l)
        return (any(f in self.source_lower for f in forms)
                or any(f in self.allowed_lower for f in forms)
                or (" " not in term_l and _stem(term_l) in self.stems))


def _unverified(token: str, at_start: bool, evidence: _Evidence) -> bool:
    if "-" not in token:
        return _tech_like(token, at_start) and not evidence.verifies(token)
    if evidence.verifies(token):
        return False                             # found whole: "Groq-hosted"
    for i, part in enumerate(p for p in token.split("-") if p):
        if part.isalpha() and part.islower():
            continue                             # "edge", "based", "making"
        if _tech_like(part, at_start and i == 0) and not evidence.verifies(part):
            return True
    return False


def validate_letter_paragraph(paragraph: str, source_text: str,
                              allowed_skills=()) -> dict:
    """Check one cover-letter paragraph against everything the candidate
    can claim (cover_letter._profile_evidence_text). Returns {"ok": bool,
    "flagged_terms": [...], "flagged_numbers": [...]}, like validate_bullet."""
    evidence = _Evidence(source_text, allowed_skills)
    flagged_terms = {m.group(0) for m in _TERM_RE.finditer(paragraph)
                     if _unverified(m.group(0), _at_sentence_start(paragraph, m.start()),
                                    evidence)}

    # Numbers: exactly validate_bullet's check.
    source_numeric = {re.sub(r"[^\d.]", "", n) for n in _extract_numbers(source_text)}
    flagged_numbers = set()
    for num in _extract_numbers(paragraph):
        numeric = re.sub(r"[^\d.]", "", num)
        if numeric and numeric not in source_numeric:
            flagged_numbers.add(num)

    return {"ok": not flagged_terms and not flagged_numbers,
            "flagged_terms": sorted(flagged_terms),
            "flagged_numbers": sorted(flagged_numbers)}
