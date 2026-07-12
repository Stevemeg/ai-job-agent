from backend.analysis.career_recommender import (recommend_careers,
                                                 ROLE_ARCHETYPES,
                                                 ROLE_TITLE_KEYWORDS)


def test_fit_bounds_and_sorting(profile):
    careers = recommend_careers(profile)
    assert careers, "profile with AI skills must match some archetype"
    fits = [c["fit_pct"] for c in careers]
    assert all(0 <= f <= 100 for f in fits)
    assert fits == sorted(fits, reverse=True)


def test_evidence_and_missing_are_disjoint_core_partition(profile):
    for c in recommend_careers(profile):
        core = set(ROLE_ARCHETYPES[c["role"]]["core"])
        evid = set(c["evidence"])
        missing = set(c["missing"])
        assert missing <= core                      # missing only from core
        assert not (evid & missing)                 # never both
        assert (evid & core) | missing == core      # partition of core


def test_ai_profile_ranks_ai_roles_over_analyst(profile):
    careers = {c["role"]: c["fit_pct"] for c in recommend_careers(profile, top_n=8)}
    assert careers.get("AI Engineer", 0) > careers.get("Data Analyst", 0)


def test_empty_profile_returns_empty():
    assert recommend_careers({"skills": [], "projects": []}) == []


def test_every_archetype_has_title_keywords():
    """skill_gap's role weighting needs keywords for every archetype."""
    for role in ROLE_ARCHETYPES:
        assert ROLE_TITLE_KEYWORDS.get(role), f"missing title keywords: {role}"
