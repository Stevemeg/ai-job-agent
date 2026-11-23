"""
settings.py -- the agent's AGENT_* settings, read from the environment on
every call (a changed .env takes effect with the next run, no restart).

  AGENT_ENABLED               false   scans chain an agent run when true
  AGENT_MIN_SCORE             20      triage: match_score >= this (about the
                                      90th percentile of the real corpus's
                                      eligible, gate-passing jobs;
                                      CONFIGURATION.md)
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
  AGENT_APPROVAL_TTL_DAYS     7       an application nobody approves, edits or
                                      declines within this many days expires
  AGENT_EXCLUDE_TITLE_WORDS   senior, sr, staff, principal, lead, manager,
                              director, head, architect, vp, distinguished
                                      triage: comma-separated; a job whose title
                                      contains one as a whole word is skipped
                                      (titles.py). "none" turns the filter off
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .locations import DEFAULT_LOCATIONS, LocationPolicy, words
from .titles import DEFAULT_EXCLUDE_TITLE_WORDS, TitlePolicy

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = False
    min_score: float = 20.0
    max_job_age_days: int = 14
    max_drafts_per_run: int = 5
    locations: tuple[str, ...] = DEFAULT_LOCATIONS
    accept_global_remote: bool = False
    approval_ttl_days: int = 7
    exclude_title_words: tuple[str, ...] = DEFAULT_EXCLUDE_TITLE_WORDS

    def as_dict(self) -> dict:
        out = asdict(self)
        out["locations"] = list(self.locations)
        out["exclude_title_words"] = list(self.exclude_title_words)
        return out

    def location_policy(self) -> LocationPolicy:
        return LocationPolicy(self.locations, self.accept_global_remote)

    def title_policy(self) -> TitlePolicy:
        return TitlePolicy(self.exclude_title_words)


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


def _title_words(env, default):
    """Unset or empty: the default list. "none": no exclusions. Entries
    are lower-cased, with their whitespace collapsed."""
    raw = (env.get("AGENT_EXCLUDE_TITLE_WORDS") or "").strip()
    if not raw:
        return default
    if raw.lower() == "none":
        return ()
    items = tuple(" ".join(item.split()).lower() for item in raw.split(",") if item.strip())
    bad = [item for item in items if not words(item)]
    if bad or not items:
        raise ValueError("AGENT_EXCLUDE_TITLE_WORDS entries need a letter or digit "
                         f"(or the single value 'none'), got {raw!r}")
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
        approval_ttl_days=_number(env, "AGENT_APPROVAL_TTL_DAYS",
                                  defaults.approval_ttl_days, int, 1),
        exclude_title_words=_title_words(env, defaults.exclude_title_words),
    )
