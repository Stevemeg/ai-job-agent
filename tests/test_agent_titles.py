"""The seniority filter triage applies to job titles (backend/agent/titles.py,
AGENT_EXCLUDE_TITLE_WORDS): whole words, case-insensitive, no substring
matches. Pure: no database."""
import pytest

from backend.agent.settings import settings_from_env
from backend.agent.titles import DEFAULT_EXCLUDE_TITLE_WORDS, TitlePolicy

DEFAULT = TitlePolicy()


@pytest.mark.parametrize("title, word", [
    ("Senior Staff Software Engineer", "senior"),
    ("Staff ML Engineer", "staff"),
    ("Sr. Machine Learning Engineer", "sr"),
    ("SR Data Scientist", "sr"),
    ("Principal AI Scientist", "principal"),
    ("Tech-Lead, Platform", "lead"),
    ("Team Lead - Search", "lead"),
    ("Engineering Manager, ML", "manager"),
    ("Director of Data", "director"),
    ("Head of AI", "head"),
    ("Solutions Architect (GenAI)", "architect"),
    ("VP, Engineering", "vp"),
    ("Distinguished Engineer", "distinguished"),
    ("senior backend engineer", "senior"),
    ("SENIOR BACKEND ENGINEER", "senior"),
])
def test_seniority_titles_are_excluded(title, word):
    assert DEFAULT.excluded_by(title) == word
    assert not DEFAULT.keeps(title)


@pytest.mark.parametrize("title", [
    "AI Engineer",
    "Machine Learning Engineer",
    "Leading-edge AI Engineer",        # "leading" is not "lead"
    "Leadership Development Associate",
    "Headless CMS Developer",          # "headless" is not "head"
    "Staffing Coordinator",            # "staffing" is not "staff"
    "Managerial Accountant",
    "Architecture Intern",
    "Srinagar Field Engineer",         # "srinagar" is not "sr"
    "Junior Data Scientist",
    "Software Engineer, New Grad",
    "",
    None,
])
def test_other_titles_are_kept(title):
    assert DEFAULT.excluded_by(title) is None
    assert DEFAULT.keeps(title)


def test_a_multi_word_entry_needs_its_words_in_a_row():
    policy = TitlePolicy(("vice president",))
    assert policy.excluded_by("Vice President, AI") == "vice president"
    assert policy.excluded_by("Vice-President of Data") == "vice president"
    assert policy.keeps("President's Office Vice Coordinator")


def test_an_empty_policy_keeps_everything():
    assert TitlePolicy(()).keeps("Senior Staff Principal Director")


def test_the_settings_default_and_parsing():
    assert settings_from_env({}).exclude_title_words == DEFAULT_EXCLUDE_TITLE_WORDS
    assert DEFAULT_EXCLUDE_TITLE_WORDS == (
        "senior", "sr", "staff", "principal", "lead", "manager", "director", "head",
        "architect", "vp", "distinguished")
    assert settings_from_env({"AGENT_EXCLUDE_TITLE_WORDS": ""}).exclude_title_words \
        == DEFAULT_EXCLUDE_TITLE_WORDS
    s = settings_from_env({"AGENT_EXCLUDE_TITLE_WORDS": " Senior , Vice  President,intern "})
    assert s.exclude_title_words == ("senior", "vice president", "intern")
    assert s.title_policy().excluded_by("Data Intern") == "intern"
    assert s.title_policy().keeps("Staff Engineer")
    assert s.as_dict()["exclude_title_words"] == ["senior", "vice president", "intern"]
    off = settings_from_env({"AGENT_EXCLUDE_TITLE_WORDS": "None"})
    assert off.exclude_title_words == () and off.title_policy().keeps("Senior Lead")


@pytest.mark.parametrize("raw", ["senior, ,--", ",", "!!"])
def test_malformed_title_words_are_rejected(raw):
    with pytest.raises(ValueError):
        settings_from_env({"AGENT_EXCLUDE_TITLE_WORDS": raw})
