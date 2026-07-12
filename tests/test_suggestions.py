from backend.analysis.resume_health import compute_health
from backend.analysis.suggestions import generate_suggestions


def test_severity_ordering(profile):
    health = compute_health(profile)
    sugg = generate_suggestions(profile, health)
    order = {"high": 0, "medium": 1, "low": 2}
    ranks = [order[s["severity"]] for s in sugg]
    assert ranks == sorted(ranks)


def test_unquantified_suggestion_quotes_the_bullet(profile):
    profile["projects"] = [{"title": "P", "duration": "",
                            "description": " • Worked on a novel widget thing."}]
    health = compute_health(profile)
    sugg = generate_suggestions(profile, health)
    quant = [s for s in sugg if "Quantify" in s["title"]]
    assert quant and "widget" in quant[0]["detail"]


def test_weak_verb_detected(profile):
    profile["projects"][0]["description"] = " • Worked on the RAG pipeline daily."
    health = compute_health(profile)
    sugg = generate_suggestions(profile, health)
    assert any("weak opening verbs" in s["title"].lower() for s in sugg)


def test_gap_card_uses_real_percentages(profile):
    health = compute_health(profile)
    gaps = [{"skill": "Docker", "demand_pct": 26.0, "role_demand_pct": 50.0,
             "jobs_mentioning": 26, "role_jobs": 40, "role_weighted": True,
             "priority": "High"}]
    sugg = generate_suggestions(profile, health, gaps)
    market = [s for s in sugg if s["category"] == "Market Fit"]
    assert market and "26.0%" in market[0]["detail"]
