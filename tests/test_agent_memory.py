"""Agent memory without a database (backend/agent/memory.py): what the
learning rules derive from a list of decisions, the title and location
terms they count, and how a MemorySet matches, excludes and boosts."""
from datetime import datetime, timezone

import pytest

from backend.agent import memory
from backend.agent.settings import AgentSettings, settings_from_env

SETTINGS = AgentSettings()
WHEN = datetime(2026, 11, 23, tzinfo=timezone.utc)


def _d(n, decision, company="acme", title="AI Engineer", location="Pune", reason=None):
    return {"approval_id": n, "application_id": 100 + n, "job_id": 200 + n,
            "event_id": 300 + n, "decision": decision, "company": company, "title": title,
            "location": location, "reason": reason, "decided_at": WHEN}


def test_a_company_declined_twice_and_never_approved_becomes_company_avoid():
    one = memory.derive([_d(1, "decline", reason="too far")], SETTINGS)
    assert ("company_avoid", "acme") not in one
    two = memory.derive([_d(1, "decline", reason="too far"), _d(2, "decline")], SETTINGS)
    m = two[("company_avoid", "acme")]
    assert (m["kind"], m["value"], m["weight"]) == ("company_avoid", "acme", 2.0)
    ev = m["evidence"]
    assert [e["approval_id"] for e in ev["declined"]] == [1, 2]
    assert [e["event_id"] for e in ev["declined"]] == [301, 302]
    assert [e["application_id"] for e in ev["declined"]] == [101, 102]
    assert ev["decline_reasons"] == ["too far"]
    assert ev["approved"] == [] and "never approved" in ev["rule"]
    assert ev["declined"][0]["decided_at"] == WHEN.isoformat()


def test_an_approved_company_is_never_avoided():
    out = memory.derive([_d(1, "decline"), _d(2, "decline"), _d(3, "approve")], SETTINGS)
    assert ("company_avoid", "acme") not in out


def test_a_title_word_declined_three_times_with_no_approvals_becomes_title_avoid():
    declines = [_d(i, "decline", company=f"c{i}", title=t) for i, t in enumerate(
        ["Frontend Engineer", "Frontend Developer (React)", "Software Engineer - Frontend"])]
    out = memory.derive(declines[:2], SETTINGS)
    assert ("title_avoid", "frontend") not in out
    out = memory.derive(declines, SETTINGS)
    assert out[("title_avoid", "frontend")]["weight"] == 3.0
    # Generic words are never learned, however often they are declined.
    assert not {k for k in out if k[1] in ("engineer", "developer", "software")}
    approved = declines + [_d(9, "approve", company="z", title="Frontend Intern")]
    assert ("title_avoid", "frontend") not in memory.derive(approved, SETTINGS)


def test_approvals_raise_prefer_memories():
    one = memory.derive([_d(1, "approve", title="ML Engineer", location="Remote, India")],
                        SETTINGS)
    assert not [k for k in one if k[0].endswith("_prefer")]     # one approval: not yet
    out = memory.derive([_d(1, "approve", title="ML Engineer", location="Remote, India"),
                         _d(2, "approve", title="ML Platform Engineer",
                            location="Bengaluru, India"),
                         _d(3, "approve", title="ML Engineer", location="Pune")], SETTINGS)
    assert out[("company_prefer", "acme")]["weight"] == 3.0
    assert out[("title_prefer", "ml")]["weight"] == 3.0
    assert ("title_prefer", "platform") not in out              # approved once
    assert ("location_prefer", "remote") not in out
    assert ("location_prefer", "pune") not in out
    assert ("location_prefer", "india") not in out              # never learned


def test_prefer_needs_more_approvals_than_declines_and_weight_is_the_difference():
    base = [_d(1, "approve"), _d(2, "approve"), _d(3, "decline")]
    assert memory.derive(base, SETTINGS)[("company_prefer", "acme")]["weight"] == 1.0
    assert ("company_prefer", "acme") not in memory.derive(base + [_d(4, "decline")],
                                                           SETTINGS)


def test_thresholds_come_from_the_settings():
    s = settings_from_env({"AGENT_LEARN_COMPANY_DECLINES": "1",
                           "AGENT_LEARN_TITLE_DECLINES": "1",
                           "AGENT_LEARN_PREFER_APPROVALS": "1"})
    out = memory.derive([_d(1, "decline", title="Data Analyst"),
                         _d(2, "approve", company="good", title="AI Engineer",
                            location="Hyderabad")], s)
    assert {("company_avoid", "acme"), ("title_avoid", "data"), ("title_avoid", "analyst"),
            ("company_prefer", "good"), ("title_prefer", "ai"),
            ("location_prefer", "hyderabad")} <= set(out)
    assert ("location_prefer", "pune") not in out       # declined there: no preference
    assert AgentSettings().learn_company_declines == 2
    assert AgentSettings().learn_title_declines == 3
    assert AgentSettings().learn_prefer_approvals == 2
    assert AgentSettings().memory_max_boost == 5.0
    for bad in ({"AGENT_LEARN_COMPANY_DECLINES": "0"}, {"AGENT_MEMORY_MAX_BOOST": "-1"},
                {"AGENT_LEARN_PREFER_APPROVALS": "two"}):
        with pytest.raises(ValueError):
            settings_from_env(bad)


def test_derive_is_deterministic():
    decisions = [_d(i, "decline" if i % 3 else "approve", company=f"c{i % 2}",
                    title=f"Title{i % 4} Engineer") for i in range(12)]
    assert memory.derive(decisions, SETTINGS) == memory.derive(list(decisions), SETTINGS)


def test_title_and_location_terms():
    assert memory.title_terms("Senior ML Engineer II (Bengaluru) - Search") == [
        "senior", "ml", "search"]
    assert memory.title_terms("Software Engineer") == []
    locs = SETTINGS.locations
    assert memory.location_terms("Remote, Bangalore", locs) == ["Bangalore", "remote"]
    assert memory.location_terms("Bengaluru, Karnataka, India", locs) == ["Bengaluru"]
    assert memory.location_terms("India", locs) == []
    assert memory.location_terms(None, locs) == []


def _m(id_, kind, value, weight=1.0, active=True, provenance="stated"):
    return {"id": id_, "kind": kind, "value": value, "weight": weight, "active": active,
            "provenance": provenance}


def test_a_memory_set_matches_companies_titles_and_locations():
    ms = memory.MemorySet([_m(1, "company_avoid", "Sarvam AI"),
                           _m(2, "title_avoid", "frontend"),
                           _m(3, "company_prefer", "GitLab", 2),
                           _m(4, "title_prefer", "machine learning", 1.5),
                           _m(5, "location_prefer", "remote", 1),
                           _m(6, "note", "ask about visas"),
                           _m(7, "company_avoid", "gone", active=False)], max_boost=5)
    assert len(ms) == 5 and ms.reorders
    job = dict(company="sarvam-ai", title="AI Engineer", location="Pune")
    assert ms.avoided_by(**job)["id"] == 1                     # letters/digits only
    assert ms.avoided_by(company="x", title="Frontend-Engineer", location="")["id"] == 2
    assert ms.avoided_by(company="x", title="Front End Engineer", location="") is None
    assert ms.avoided_by(company="gone", title="AI", location="") is None   # inactive
    effects = ms.effects(company="gitlab", title="Machine Learning Engineer",
                         location="Remote, India")
    assert effects["boost"] == 4.5
    assert [m["id"] for m in effects["memories"]] == [3, 4, 5]
    assert ms.effects(company="other", title="Machine Engineer", location="Pune") is None


def test_the_boost_is_capped():
    ms = memory.MemorySet([_m(1, "company_prefer", "gitlab", 4),
                           _m(2, "title_prefer", "ai", 4)], max_boost=5)
    assert ms.effects(company="gitlab", title="AI Engineer", location="")["boost"] == 5
    assert memory.MemorySet([_m(1, "company_prefer", "gitlab", 4)],
                            max_boost=0).reorders is False
    only_avoid = memory.MemorySet([_m(1, "company_avoid", "x")])
    assert len(only_avoid) == 1 and only_avoid.reorders is False


def test_company_key():
    assert memory.company_key("Sarvam AI") == memory.company_key("sarvamai") == "sarvamai"
    assert memory.company_key("Observe.AI") == "observeai"
    assert memory.company_key(None) == ""
