from backend.analysis.skill_gap import compute_skill_gaps, MARKET_SKILLS, _candidate_has


def test_candidate_has_exact_and_variants():
    skills = {"docker", "llm", "hugging face"}
    assert _candidate_has("Docker", skills)
    assert _candidate_has("LLMs", skills)            # plural tolerance
    assert _candidate_has("HuggingFace", skills)     # spacing tolerance
    assert not _candidate_has("Kubernetes", skills)


def test_owned_skills_never_reported_as_gaps(profile, jobs):
    gaps = compute_skill_gaps(profile, jobs)
    gap_names = {g["skill"].lower() for g in gaps}
    for s in profile["skills"]:
        assert s["skill"].lower() not in gap_names


def test_demand_percentages_are_real_counts(profile, jobs):
    gaps = compute_skill_gaps(profile, jobs)
    docker = next(g for g in gaps if g["skill"] == "Docker")
    # fixture: 26 of 100 jobs mention docker
    assert docker["jobs_mentioning"] == 26
    assert docker["demand_pct"] == 26.0


def test_role_weighting_with_sufficient_subset(profile, jobs):
    gaps = compute_skill_gaps(profile, jobs, target_roles=["AI Engineer"])
    docker = next(g for g in gaps if g["skill"] == "Docker")
    assert docker["role_jobs"] == 40                 # 40 AI Engineer titles
    assert docker["role_weighted"] is True           # >= 30 threshold
    assert docker["role_demand_pct"] == 50.0         # 20 of 40


def test_honesty_guard_below_threshold(profile, jobs):
    small = jobs[:10] + jobs[40:]                    # only 10 target-role jobs
    gaps = compute_skill_gaps(profile, small, target_roles=["AI Engineer"])
    docker = next(g for g in gaps if g["skill"] == "Docker")
    assert docker["role_weighted"] is False          # subset too small to trust


def test_go_regex_regression():
    """\\bgo\\b matched the English verb and faked 26% demand. Never again."""
    import re
    rx = re.compile(MARKET_SKILLS["Go"], re.IGNORECASE)
    for text in ["go to market", "we go beyond", "on the go", "let's go!"]:
        assert not rx.search(text), text
    for text in ["golang expert", "Go developer", "go programming language"]:
        assert rx.search(text), text


def test_empty_jobs_returns_empty(profile):
    assert compute_skill_gaps(profile, []) == []


def test_sorted_by_relevance_desc(profile, jobs):
    gaps = compute_skill_gaps(profile, jobs, target_roles=["AI Engineer"])
    ranks = [g["role_demand_pct"] if g["role_weighted"] else g["demand_pct"]
             for g in gaps]
    assert ranks == sorted(ranks, reverse=True)
