"""
skill_categories.py -- Phase 4, Mode B: category-level (is-a) skill matching.

DIFFERENT from skill_taxonomy.py's SKILL_ALIASES, which handles EXACT
synonyms ("RAG" IS "Retrieval-Augmented Generation" -- same thing, different
words, symmetric, full confidence).

This module handles a WEAKER, ONE-DIRECTIONAL relationship: a specific tool
the candidate has used IS AN EXAMPLE of a broader category a JD might
mention instead. "CLIP" IS-A "vision-language model" -- but if a JD just
says "vision-language model", that's broader than CLIP specifically. It's
real evidence, but weaker than an exact mention, so it should NOT get full
credit in scoring (see CATEGORY_MATCH_CREDIT below).

SCOPE: deliberately limited to 4 skills where the category phrasing is
genuinely common in real JDs -- CLIP, DistilBERT, GANs, Transformers. Other
candidate skills (FAISS, Streamlit, NumPy, etc.) don't have an established,
commonly-used broader category that a JD would plausibly write instead of
the literal tool name, so they're left out rather than inventing a stretch
mapping that would dilute scoring honesty. CNNs is correctly NOT here --
"CNN" is an acronym for "Convolutional Neural Network", which is an exact
synonym handled in skill_taxonomy.py's SKILL_ALIASES, not a category
relationship.
"""

# Credit given for a category-level match, relative to 1.0 for an exact/
# alias match. Chosen to be meaningfully less than full credit (a JD asking
# for "vision-language models" in general is a weaker, broader signal than
# a JD that names CLIP specifically) while still being substantive enough
# to matter in scoring.
CATEGORY_MATCH_CREDIT = 0.6

# canonical_skill (lowercase) -> set of category phrases (lowercase) that,
# if present in a JD, count as partial evidence the candidate's specific
# skill is relevant -- even though the JD never names the skill itself.
SKILL_CATEGORIES = {
    "clip": {
        "vision-language model", "vision-language models",
        "image-text model", "image-text models",
        "multimodal embedding", "multimodal embeddings",
    },
    "distilbert": {
        "transformer-based nlp", "bert-based model", "bert-based models",
        "pretrained language model", "pretrained language models",
    },
    "gans": {
        "adversarial training", "image generation model",
        "image generation models", "generative adversarial",
    },
    "transformers": {
        "attention-based model", "attention-based models",
        "self-attention architecture", "transformer architecture",
    },
}


def get_category_phrases(skill_name):
    """Returns the set of broader category phrases this skill is an
    example of, or an empty set if this skill has no category mapping."""
    key = (skill_name or "").lower().strip()
    return SKILL_CATEGORIES.get(key, set())