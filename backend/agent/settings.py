"""
settings.py -- the agent's AGENT_* settings, read from the environment on
every call (a changed .env takes effect with the next run, no restart).

  AGENT_ENABLED               false   scans chain an agent run when true
  AGENT_MIN_SCORE             80      triage: match_score >= this
  AGENT_MAX_JOB_AGE_DAYS      14      triage: job last seen within this many days
  AGENT_MAX_DRAFTS_PER_RUN    5       triage: at most this many new applications
  AGENT_LOCATIONS             Bengaluru, Bangalore, Hyderabad, Pune, Chennai,
                              Mumbai, Gurgaon, Gurugram, Noida, Delhi, NCR,
                              India
                                      triage: comma-separated; a job's location
                                      (or a posting's) must contain one, as
                                      whole words (locations.py)
  AGENT_ACCEPT_GLOBAL_REMOTE  false   triage: true also accepts remote jobs
                                      that don't mention India
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .locations import DEFAULT_LOCATIONS, LocationPolicy, words

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = False
    min_score: float = 80.0
    max_job_age_days: int = 14
    max_drafts_per_run: int = 5
    locations: tuple[str, ...] = DEFAULT_LOCATIONS
    accept_global_remote: bool = False

    def as_dict(self) -> dict:
        out = asdict(self)
        out["locations"] = list(self.locations)
        return out

    def location_policy(self) -> LocationPolicy:
        return LocationPolicy(self.locations, self.accept_global_remote)


def _bool(env, name, default):
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw not in _TRUE | _FALSE:
        raise ValueError(f"{name} must be true or false, got {raw!r}")
    return raw in _TRUE


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


def _locations(env, default):
    raw = env.get("AGENT_LOCATIONS") or ""
    items = tuple(" ".join(item.split()) for item in raw.split(",") if item.strip())
    if not items:
        return default
    bad = [item for item in items if not words(item)]
    if bad:
        raise ValueError(f"AGENT_LOCATIONS entries need a letter or digit, got {bad!r}")
    return items


def settings_from_env(env: dict | None = None) -> AgentSettings:
    """The current settings, validated (ValueError on a malformed value)."""
    env = os.environ if env is None else env
    defaults = AgentSettings()
    return AgentSettings(
        enabled=_bool(env, "AGENT_ENABLED", defaults.enabled),
        min_score=_number(env, "AGENT_MIN_SCORE", defaults.min_score, float, 0),
        max_job_age_days=_number(env, "AGENT_MAX_JOB_AGE_DAYS",
                                 defaults.max_job_age_days, int, 1),
        max_drafts_per_run=_number(env, "AGENT_MAX_DRAFTS_PER_RUN",
                                   defaults.max_drafts_per_run, int, 0),
        locations=_locations(env, defaults.locations),
        accept_global_remote=_bool(env, "AGENT_ACCEPT_GLOBAL_REMOTE",
                                   defaults.accept_global_remote),
    )
