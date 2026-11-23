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

3. Known entities. Rule 1 alone would let a name slip through by starting
   a sentence ("Google hired me as an intern."). So a sentence-initial
   word -- or its first one to three words, for names like "Tata
   Consultancy Services", or the first part of a hyphenated token
   ("Kafka-backed") -- that is a KNOWN ENTITY is flagged unless the
   profile evidence has it or it is the target company. Known entities:
     - every company in the job corpus (canonical_jobs; the JSON corpus
       without a database), read through corpus_companies() and cached
       for CORPUS_CACHE_S,
     - the skill taxonomy (skill_taxonomy.SKILL_ALIASES, names and
       aliases),
     - KNOWN_COMPANIES and KNOWN_TOOLS below: a short curated list of big
       companies and widely used tools.
   Names compare on letters and digits only ("Hugging Face" =
   "huggingface"). The cost: a corpus company that is also an English
   word ("Linear", "Modal", "Ramp") is flagged when it starts a sentence
   and the profile doesn't mention it. A name in none of the three lists
   that starts a sentence is still not flagged; mid-sentence, any
   capitalized name is. tests/fixtures/letter_validator/ records both.
"""
from __future__ import annotations

import re
import threading
import time

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

# Rule 3's curated lists (rule 3 also uses the corpus and the taxonomy).
KNOWN_COMPANIES = (
    "Google", "Alphabet", "DeepMind", "Microsoft", "Apple", "Amazon", "AWS", "Meta",
    "Facebook", "Instagram", "WhatsApp", "Netflix", "NVIDIA", "Intel", "AMD", "IBM",
    "Oracle", "Salesforce", "Adobe", "Uber", "Tesla", "Twitter", "LinkedIn", "Samsung",
    "Qualcomm", "Cisco", "SAP", "VMware", "OpenAI", "Anthropic", "Hugging Face",
    "Mistral AI", "Cohere", "Infosys", "TCS", "Tata Consultancy Services", "Wipro",
    "HCL", "Accenture", "Deloitte", "Capgemini", "Cognizant", "Flipkart", "Paytm",
    "Swiggy", "Zomato", "Razorpay", "PhonePe")
KNOWN_TOOLS = (
    "Kubernetes", "Docker", "Kafka", "Spark", "Hadoop", "Airflow", "Snowflake", "Tableau",
    "Terraform", "Jenkins", "TensorFlow", "PyTorch", "Keras", "LangChain", "LlamaIndex",
    "MongoDB", "PostgreSQL", "MySQL", "Redis", "Elasticsearch", "Azure", "GCP",
    "BigQuery", "Databricks", "Pinecone", "Weaviate", "Kotlin", "Golang", "Django",
    "FastAPI", "React", "Angular")
CORPUS_CACHE_S = 600.0
_MAX_NAME_WORDS = 3

__all__ = ["validate_letter_paragraph", "known_entities", "corpus_companies",
           "entity_key", "KNOWN_COMPANIES", "KNOWN_TOOLS"]


def entity_key(name) -> str:
    """A name on its letters and digits, case-folded: "Hugging Face",
    "hugging-face" and "huggingface" are one key."""
    return re.sub(r"[^0-9a-z]", "", str(name or "").casefold())


_STATIC_KEYS = frozenset(k for k in map(entity_key, (*KNOWN_COMPANIES, *KNOWN_TOOLS,
                                                    *_TAXONOMY)) if k)
_corpus_lock = threading.Lock()
_corpus_cache = None                    # (monotonic time, frozenset of names)


def _corpus_companies() -> frozenset:
    """Every company in the job corpus: canonical_jobs when a database is
    configured and reachable, else the JSON corpus. Cached for
    CORPUS_CACHE_S. Never raises: no corpus means no corpus names."""
    global _corpus_cache
    now = time.monotonic()
    cached = _corpus_cache
    if cached is not None and now - cached[0] < CORPUS_CACHE_S:
        return cached[1]
    with _corpus_lock:
        try:
            from ..database import corpus
            if corpus.db_available():
                from sqlalchemy import text
                from ..database.db import get_engine
                with get_engine().connect() as conn:
                    names = conn.execute(text(
                        "SELECT DISTINCT company FROM canonical_jobs "
                        "WHERE content_hash IS NOT NULL")).scalars().all()
            else:
                names = [j.get("company") for j in corpus.load_corpus_snapshot().jobs]
            found = frozenset(n for n in names if n)
        except Exception:                                   # noqa: BLE001
            found = frozenset()
        _corpus_cache = (now, found)
        return found


# Indirection so tests can pin the corpus part (tests/conftest.py does).
corpus_companies = _corpus_companies


def known_entities(extra=()) -> frozenset:
    """Rule 3's keys: the curated lists, the taxonomy, the corpus's
    companies and `extra` names."""
    return _STATIC_KEYS | frozenset(
        k for k in map(entity_key, (*corpus_companies(), *extra)) if k)


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


def _entity_at(paragraph: str, tokens: list, i: int, entities: frozenset):
    """Rule 3: the known entity starting at token i, as (name as written,
    index of its last token) -- the longest of its first 1..3 words
    (joined by spaces only) or, for a hyphenated token, its first part
    (the whole token is the name). None if no known entity starts there."""
    first = tokens[i].group(0)
    if not first[:1].isupper():
        return None
    if "-" in first:
        return (first, i) if entity_key(first.split("-", 1)[0]) in entities else None
    best = None
    for j in range(i, min(i + _MAX_NAME_WORDS, len(tokens))):
        if j > i and paragraph[tokens[j - 1].end():tokens[j].start()].strip():
            break                                # "Google, ..." ends the name
        name = paragraph[tokens[i].start():tokens[j].end()]
        if entity_key(name) in entities:
            best = (name, j)
    return best


def validate_letter_paragraph(paragraph: str, source_text: str, allowed_skills=(), *,
                              target_company=None, entities=None) -> dict:
    """Check one cover-letter paragraph against everything the candidate
    can claim (cover_letter._profile_evidence_text). The target company
    is never a rule-3 flag (a letter names it); `entities` defaults to
    known_entities(). Returns {"ok": bool, "flagged_terms": [...],
    "flagged_numbers": [...]}, like validate_bullet."""
    evidence = _Evidence(source_text, allowed_skills)
    entities = known_entities() if entities is None else entities
    target = entity_key(target_company)
    tokens = list(_TERM_RE.finditer(paragraph))
    flagged_terms = set()
    i = 0
    while i < len(tokens):
        token = tokens[i].group(0)
        at_start = _at_sentence_start(paragraph, tokens[i].start())
        found = _entity_at(paragraph, tokens, i, entities)
        # A known name is one term: at a sentence start any known name
        # (rule 3), elsewhere a multi-word one ("at Hugging Face" is one
        # claim, not "Hugging" and "Face").
        if found is not None and (at_start or " " in found[0]):
            name, last = found
            hyphenated = " " not in name and "-" in name
            head = name.split("-", 1)[0] if hyphenated else name
            if (entity_key(head) != target and not evidence.verifies(head)) or (
                    hyphenated and _unverified(name, at_start, evidence)):
                flagged_terms.add(name)          # (a hyphenated one: every part checked)
            i = last + 1
            continue
        if _unverified(token, at_start, evidence):
            flagged_terms.add(token)
        i += 1

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
