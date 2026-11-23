"""AGENT_* settings: defaults, parsing, and rejection of malformed values."""
import pytest

from backend.agent.settings import AgentSettings, settings_from_env


def test_defaults():
    assert settings_from_env({}) == AgentSettings(
        enabled=False, min_score=20.0, max_job_age_days=14, max_drafts_per_run=5)


def test_the_min_score_default_is_20():
    # Phase 6 review decision: ~p90 of the real corpus's location-eligible,
    # gate-passing scores (max 30.38) -- 80 triaged nothing at all.
    assert AgentSettings().min_score == 20.0
    assert settings_from_env({"AGENT_MIN_SCORE": ""}).min_score == 20.0


def test_values_are_parsed():
    s = settings_from_env({"AGENT_ENABLED": "True", "AGENT_MIN_SCORE": "72.5",
                           "AGENT_MAX_JOB_AGE_DAYS": "3",
                           "AGENT_MAX_DRAFTS_PER_RUN": "0"})
    assert s == AgentSettings(True, 72.5, 3, 0)
    assert settings_from_env({"AGENT_ENABLED": "off"}).enabled is False
    assert settings_from_env({"AGENT_ENABLED": " 1 "}).enabled is True


@pytest.mark.parametrize("env", [
    {"AGENT_ENABLED": "maybe"},
    {"AGENT_MIN_SCORE": "high"},
    {"AGENT_MIN_SCORE": "-1"},
    {"AGENT_MAX_JOB_AGE_DAYS": "0"},
    {"AGENT_MAX_JOB_AGE_DAYS": "1.5"},
    {"AGENT_MAX_DRAFTS_PER_RUN": "-2"},
])
def test_malformed_values_are_rejected(env):
    with pytest.raises(ValueError):
        settings_from_env(env)
