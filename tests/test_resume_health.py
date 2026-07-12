from backend.analysis.resume_health import compute_health, _grade


def test_score_bounds_and_grade(profile):
    h = compute_health(profile)
    assert 0 <= h["score"] <= 100
    assert h["grade"] in "ABCDE"
    assert h["grade"] == _grade(h["score"])


def test_dimensions_capped_at_max(profile):
    h = compute_health(profile)
    for dim, d in h["breakdown"].items():
        assert 0 <= d["score"] <= d["max"], dim
    assert sum(d["max"] for d in h["breakdown"].values()) == 100


def test_full_profile_scores_high(profile):
    assert compute_health(profile)["score"] >= 70


def test_missing_contact_penalized_with_findings(profile):
    profile["email"] = None
    profile["github"] = None
    h = compute_health(profile)
    contact = h["breakdown"]["Contact & Links"]
    assert contact["score"] < contact["max"]
    assert any("Email" in f for f in contact["findings"])


def test_empty_profile_scores_low():
    h = compute_health({"name": "", "skills": [], "education": [], "projects": []})
    assert h["score"] < 20
    assert h["grade"] == "E"


def test_unquantified_bullets_detected(profile):
    profile["projects"] = [{"title": "P", "duration": "",
                            "description": " • Built a thing. • Made another thing."}]
    h = compute_health(profile)
    qi = h["breakdown"]["Quantified Impact"]
    assert qi["score"] == 0
    assert qi["findings"]


def test_grade_boundaries():
    assert _grade(85) == "A"
    assert _grade(84.9) == "B"
    assert _grade(70) == "B"
    assert _grade(55) == "C"
    assert _grade(40) == "D"
    assert _grade(39.9) == "E"
