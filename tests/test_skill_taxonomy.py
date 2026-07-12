from backend.matching_engine.skill_taxonomy import SKILL_ALIASES
from backend.matching_engine.skill_categories import (SKILL_CATEGORIES,
                                                      CATEGORY_MATCH_CREDIT,
                                                      get_category_phrases)


def test_aliases_are_lowercase():
    for canonical, aliases in SKILL_ALIASES.items():
        assert canonical == canonical.lower()
        for a in aliases:
            assert a == a.lower(), f"{canonical}: {a}"


def test_key_symmetric_pairs_present():
    assert "retrieval-augmented generation" in SKILL_ALIASES["rag"]
    assert "rag" in SKILL_ALIASES["retrieval-augmented generation"]


def test_category_credit_is_partial():
    """The 0.6 partial credit is a documented scoring-honesty decision --
    category evidence must never earn full credit."""
    assert 0 < CATEGORY_MATCH_CREDIT < 1


def test_category_lookup():
    assert "vision-language model" in get_category_phrases("CLIP")
    assert get_category_phrases("nonexistent") == set()


def test_no_overlap_between_alias_and_category_layers():
    """A skill handled as an exact alias must not also give category credit
    for the same phrase -- that would double-count."""
    for skill, phrases in SKILL_CATEGORIES.items():
        aliases = SKILL_ALIASES.get(skill, set())
        assert not (phrases & aliases), skill
