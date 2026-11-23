"""
settings.py -- the agent's AGENT_* settings, read from the environment on
every call (a changed .env takes effect with the next run, no restart).

  AGENT_ENABLED             false   scans chain an agent run when true
  AGENT_MIN_SCORE           80      triage: match_score >= this
  AGENT_MAX_JOB_AGE_DAYS    14      triage: job last seen within this many days
  AGENT_MAX_DRAFTS_PER_RUN  5       triage: at most this many new applications
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = False
    min_score: float = 80.0
    max_job_age_days: int = 14
    max_drafts_per_run: int = 5

    def as_dict(self) -> dict:
        return asdict(self)


def _number(env, name, default, kind, minimum):
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise ValueError(f"{name} must be a{'n integer' if kind is int else ' number'}, "
                         f"got {raw!r}") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {raw!r}")
    return value


def settings_from_env(env: dict | None = None) -> AgentSettings:
    """The current settings, validated (ValueError on a malformed value)."""
    env = os.environ if env is None else env
    raw = (env.get("AGENT_ENABLED") or "").strip().lower()
    if raw not in _TRUE | _FALSE:
        raise ValueError(f"AGENT_ENABLED must be true or false, got {raw!r}")
    defaults = AgentSettings()
    return AgentSettings(
        enabled=raw in _TRUE,
        min_score=_number(env, "AGENT_MIN_SCORE", defaults.min_score, float, 0),
        max_job_age_days=_number(env, "AGENT_MAX_JOB_AGE_DAYS",
                                 defaults.max_job_age_days, int, 1),
        max_drafts_per_run=_number(env, "AGENT_MAX_DRAFTS_PER_RUN",
                                   defaults.max_drafts_per_run, int, 0),
    )
