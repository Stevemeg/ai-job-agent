from backend.analysis.parse_confidence import compute_parse_confidence


def test_clean_profile_high_confidence(profile):
    pc = compute_parse_confidence(profile)
    assert pc["overall"] >= 85
    for s in pc["sections"].values():
        assert 0 <= s["confidence"] <= 100


def test_malformed_email_flagged(profile):
    profile["email"] = "not-an-email"
    pc = compute_parse_confidence(profile)
    assert pc["sections"]["Contact"]["confidence"] < 100
    assert any("malformed" in w for w in pc["sections"]["Contact"]["warnings"])


def test_empty_project_title_flagged(profile):
    profile["projects"][0]["title"] = ""
    pc = compute_parse_confidence(profile)
    assert any("title looks wrong" in w
               for w in pc["sections"]["Projects"]["warnings"])


def test_boundary_bleed_detected(profile):
    """The real bug this module caught in production data: project N's
    description containing project N+1's title."""
    profile["projects"][0]["description"] += " GAN Image Synthesizer LINK"
    pc = compute_parse_confidence(profile)
    assert any("bleed" in w for w in pc["sections"]["Projects"]["warnings"])


def test_no_projects_zero_confidence(profile):
    profile["projects"] = []
    pc = compute_parse_confidence(profile)
    assert pc["sections"]["Projects"]["confidence"] == 0
