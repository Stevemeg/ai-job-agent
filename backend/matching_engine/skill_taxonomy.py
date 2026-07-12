"""
skill_taxonomy.py -- Phase 4: synonym/alias resolution for skill matching.

WHY THIS EXISTS: token matching (scorer.skill_overlap_score,
explainer.explain_match) treats "RAG" and "Retrieval-Augmented Generation"
as two completely unrelated strings, even though they're the same skill.
Confirmed via this candidate's OWN profile, which contains both "RAG" and
"Retrieval-Augmented Generation" as separate list entries, and both
"LLM"/"LLMs" and "GAN"/"GANs" as singular/plural pairs. A JD that only
writes out "Retrieval-Augmented Generation" in full currently scores ZERO
overlap against a candidate who lists "RAG", despite being the exact same
skill.

DESIGN: each canonical skill maps to every surface form that should count
as evidence of it. Matching checks ALL surface forms, not just the literal
skill string from the candidate's profile. This is intentionally a flat,
hand-curated dictionary (not a general NLP/embedding solution) -- precise,
fast, and fully testable, covering the ~24 skills in this candidate's
profile plus their common variants. Extend it as new skills get added to
the candidate profile or new JD phrasings are observed.
"""

import re

# canonical_skill (lowercase) -> set of surface forms (lowercase) that count
# as evidence of that skill appearing in a job's tags/description.
# The canonical skill's own name is always included automatically below,
# so it doesn't need to be repeated in its own alias list.
SKILL_ALIASES = {
    "rag": {"retrieval-augmented generation", "retrieval augmented generation"},
    "llm": {"llms", "large language model", "large language models"},
    "llms": {"llm", "large language model", "large language models"},
    "gan": {"gans", "generative adversarial network", "generative adversarial networks"},
    "gans": {"gan", "generative adversarial network", "generative adversarial networks"},
    "cnns": {"cnn", "convolutional neural network", "convolutional neural networks"},
    "huggingface": {"hugging face"},
    "sentence transformers": {"sentence-transformers", "sbert"},
    "prompt engineering": {"prompt design", "prompt tuning"},
    "retrieval-augmented generation": {"rag"},
    "opencv": {"cv2"},
    "scikit-learn": {"sklearn", "scikit learn"},
    "numpy": {"numerical python"},
    "flask": {"flask api", "flask app"},
    "streamlit": {"streamlit app", "streamlit dashboard"},
    "github": {"git hub"},
    "multimodal learning": {"multimodal"},
}
# Removed in cleanup (these were CATEGORY relationships, not exact synonyms,
# and were producing false 1.0-credit matches -- e.g. FAISS getting full
# credit for a JD that only said "vector search" in general):
#   faiss -> vector search/database/store (FAISS is A vector search library,
#            not synonymous with the general concept)
#   clip, distilbert, transformers -> now correctly handled as CATEGORY
#            matches in skill_categories.py instead (partial credit, not full)
#   semantic search -> vector search/embedding search (related techniques,
#            not exact synonyms of "semantic search" specifically)
#   github/git -> "version control" (the general category they belong to,
#            not a synonym for either tool specifically)
#   ollama -> "local llm inference" (Ollama is A tool for this, not
#            synonymous with the general concept)


def get_all_surface_forms(skill_name):
    """Returns the set of all strings that should count as evidence of this
    skill, including the skill's own name. Case-insensitive lookup."""
    key = (skill_name or "").lower().strip()
    forms = {key}
    forms |= SKILL_ALIASES.get(key, set())
    return forms


def any_surface_form_present(skill_name, text):
    """True if any surface form of skill_name appears in text as a whole
    word/phrase -- NOT as a bare substring. Plain substring containment
    would let "rag" match inside "rags", "fragrance", "storage", etc, which
    is a real false positive (confirmed: a job description about selling
    "vintage area rags" scored a false RAG match before this fix). Word
    boundaries (\\b) prevent that while still matching multi-word phrases
    like "retrieval-augmented generation" correctly.
    """
    text = (text or "").lower()
    for form in get_all_surface_forms(skill_name):
        if not form:
            continue
        pattern = r'\b' + re.escape(form) + r'\b'
        if re.search(pattern, text):
            return True
    return False