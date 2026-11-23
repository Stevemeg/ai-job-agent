"""The agent's location rules (backend/agent/locations.py): which locations
triage accepts, and which posting an application uses."""
import pytest

from backend.agent.locations import (DEFAULT_LOCATIONS, LocationPolicy,
                                     job_eligibility, words)
from backend.agent.settings import settings_from_env

DEFAULT = LocationPolicy()
GLOBAL = LocationPolicy(accept_global_remote=True)


@pytest.mark.parametrize("location", [
    "Bengaluru, India", "bengaluru", "  BANGALORE  ", "Hyderabad, Telangana",
    "Pune", "Chennai, TN", "Mumbai (Hybrid)", "Gurgaon", "Gurugram, Haryana",
    "Noida", "New Delhi", "Delhi NCR", "India", "San Francisco, CA | Bengaluru",
    "Remote, India", "Remote - India", "India (Remote)", "remote/INDIA",
])
def test_listed_locations_and_india_remote_are_eligible(location):
    assert DEFAULT.eligible(location)


@pytest.mark.parametrize("location", [
    "Remote", "remote", "Remote - USA", "Remote, US", "Remote U.S.", "US Remote",
    "Remote - EU", "Remote, Europe", "Anywhere", "Worldwide",
    "San Francisco", "London, UK", "Singapore", "Indianapolis, IN",
    "Delhiwala Street", "N/A", "", None,
])
def test_everything_else_is_ineligible_by_default(location):
    assert not DEFAULT.eligible(location)


@pytest.mark.parametrize("location", ["Remote", "Remote - USA", "Remote, EU",
                                      "Worldwide", "Anywhere"])
def test_global_remote_only_with_the_opt_in(location):
    assert not DEFAULT.eligible(location)
    assert GLOBAL.eligible(location)


@pytest.mark.parametrize("location", ["San Francisco", "London, UK", "", None])
def test_the_opt_in_never_admits_an_onsite_location_elsewhere(location):
    assert not GLOBAL.eligible(location)


def test_word_match_is_case_and_whitespace_insensitive_and_multiword():
    policy = LocationPolicy(locations=("New  York", "Pune"))
    assert words("  New\tYORK , NY ") == ("new", "york", "ny")
    assert policy.eligible("new york city")
    assert not policy.eligible("York, UK")          # "new york" needs both words
    assert not policy.eligible("Newark")
    assert policy.eligible("PUNE")


def test_remote_india_qualifies_even_when_india_is_not_listed():
    policy = LocationPolicy(locations=("Pune",))
    assert policy.eligible("Remote - India")
    assert not policy.eligible("Bengaluru, India")  # not listed, not remote


def test_a_job_qualifies_through_any_posting_and_uses_that_posting():
    postings = [
        {"source": "greenhouse", "posting_id": "1", "location": "Remote - USA",
         "apply_link": "https://x/us"},
        {"source": "greenhouse", "posting_id": "2", "location": "Bengaluru",
         "apply_link": "https://x/blr"},
    ]
    fit = job_eligibility("San Francisco", postings, DEFAULT)
    assert fit.eligible and fit.match == "listed_location"
    assert fit.posting["apply_link"] == "https://x/blr"


def test_the_strongest_posting_wins_then_the_first():
    postings = [
        {"posting_id": "1", "location": "Remote, India", "apply_link": "https://x/ri"},
        {"posting_id": "2", "location": "Hyderabad", "apply_link": "https://x/hyd"},
        {"posting_id": "3", "location": "Pune", "apply_link": "https://x/pune"},
    ]
    # Default list: "India" is listed, so all three are listed matches and
    # the first posting wins the tie.
    assert job_eligibility(None, postings, DEFAULT).posting["posting_id"] == "1"
    # Without "India" listed, remote India is a weaker match than a city.
    cities = LocationPolicy(locations=("Hyderabad", "Pune"))
    assert job_eligibility(None, postings, cities).posting["posting_id"] == "2"
    assert job_eligibility(None, postings[:1], cities).match == "remote_india"


def test_a_matching_posting_is_used_even_when_the_job_location_also_matches():
    postings = [{"posting_id": "9", "location": "Bengaluru, India",
                 "apply_link": "https://x/9"}]
    fit = job_eligibility("Bengaluru", postings, DEFAULT)
    assert fit.posting["apply_link"] == "https://x/9"


def test_the_job_location_alone_needs_no_posting():
    fit = job_eligibility("Mumbai", [], DEFAULT)
    assert (fit.eligible, fit.match, fit.posting) == (True, "listed_location", None)
    # A weaker posting does not override a strictly stronger own location.
    fit = job_eligibility("Chennai", [{"posting_id": "1", "location": "Remote"}],
                          GLOBAL)
    assert (fit.match, fit.posting) == ("listed_location", None)


def test_no_qualifying_location_anywhere():
    postings = [{"posting_id": "1", "location": "Remote - US"},
                {"posting_id": "2", "location": "Toronto"}]
    assert job_eligibility("San Francisco", postings, DEFAULT).eligible is False
    assert job_eligibility(None, None, DEFAULT).eligible is False


def test_settings_defaults_and_parsing():
    s = settings_from_env({})
    assert s.locations == DEFAULT_LOCATIONS and s.accept_global_remote is False
    s = settings_from_env({"AGENT_LOCATIONS": " Pune ,  New   York,, ",
                           "AGENT_ACCEPT_GLOBAL_REMOTE": "true"})
    assert s.locations == ("Pune", "New York") and s.accept_global_remote is True
    assert s.location_policy().eligible("remote, us")
    assert settings_from_env({"AGENT_LOCATIONS": " , "}).locations == DEFAULT_LOCATIONS
    assert s.as_dict()["locations"] == ["Pune", "New York"]


@pytest.mark.parametrize("env", [{"AGENT_ACCEPT_GLOBAL_REMOTE": "sometimes"},
                                 {"AGENT_LOCATIONS": "Pune, ---"}])
def test_malformed_location_settings_are_rejected(env):
    with pytest.raises(ValueError):
        settings_from_env(env)
