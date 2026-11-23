"""
locations.py -- where the agent may draft for: the location rules triage
applies (AGENT_LOCATIONS, AGENT_ACCEPT_GLOBAL_REMOTE). Scoring and ranking
never look at this; only the agent's triage does.

A location string qualifies when, compared word by word (case and
whitespace ignored, punctuation splits words):

  1. it contains one of AGENT_LOCATIONS as whole words ("Bengaluru,
     India" has "bengaluru"; "Indianapolis" does not have "india"), or
  2. it is remote ("remote", "anywhere", "worldwide", "wfh") AND mentions
     India -- or, only with AGENT_ACCEPT_GLOBAL_REMOTE=true, any remote.

So plain "Remote", "Remote - USA" and "Remote, EU" do not qualify by
default. An empty location never qualifies.

A job qualifies when its own location or any of its postings' does. When
a posting qualifies, the application uses that posting (its location and
apply_link): the strongest match wins (a listed location, then remote
India, then global remote), the first posting on a tie. The job's own
location is used instead only if it matches strictly more strongly than
every posting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

DEFAULT_LOCATIONS = ("Bengaluru", "Bangalore", "Hyderabad", "Pune", "Chennai",
                     "Mumbai", "Gurgaon", "Gurugram", "Noida", "Delhi", "NCR",
                     "India")
REMOTE_WORDS = frozenset({"remote", "anywhere", "worldwide", "wfh"})

# Match strengths, strongest first; 0 = does not qualify.
LISTED, REMOTE_INDIA, GLOBAL_REMOTE = 3, 2, 1
NO_MATCH = 0
MATCH_NAMES = {LISTED: "listed_location", REMOTE_INDIA: "remote_india",
               GLOBAL_REMOTE: "global_remote"}

_WORD = re.compile(r"[^\W_]+")


def words(value: Any) -> tuple[str, ...]:
    """The lower-cased words of a location string ('' for None)."""
    return tuple(_WORD.findall(str(value or "").casefold()))


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    n = len(needle)
    return n > 0 and any(haystack[i:i + n] == needle
                         for i in range(len(haystack) - n + 1))


@dataclass(frozen=True)
class LocationPolicy:
    locations: tuple[str, ...] = DEFAULT_LOCATIONS
    accept_global_remote: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "_needles",
                           tuple(w for w in (words(loc) for loc in self.locations) if w))

    def strength(self, location: Any) -> int:
        """How well one location string qualifies (LISTED, REMOTE_INDIA,
        GLOBAL_REMOTE), or NO_MATCH."""
        w = words(location)
        if not w:
            return NO_MATCH
        if any(_contains(w, needle) for needle in self._needles):
            return LISTED
        if REMOTE_WORDS.intersection(w):
            if "india" in w:
                return REMOTE_INDIA
            if self.accept_global_remote:
                return GLOBAL_REMOTE
        return NO_MATCH

    def eligible(self, location: Any) -> bool:
        return self.strength(location) > NO_MATCH


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    match: str | None = None        # MATCH_NAMES value, None if not eligible
    posting: dict | None = None     # the qualifying posting the application uses


def job_eligibility(location: Any, postings: Iterable[dict] | None,
                    policy: LocationPolicy) -> Eligibility:
    """Whether a job (its own location + its postings) qualifies, and which
    posting the application should use (None: the job's own location and
    apply_link)."""
    own = policy.strength(location)
    best, best_posting = NO_MATCH, None
    for p in postings or []:
        if not isinstance(p, dict):
            continue
        s = policy.strength(p.get("location"))
        if s > best:
            best, best_posting = s, p
    if best_posting is not None and best >= own:
        return Eligibility(True, MATCH_NAMES[best], dict(best_posting))
    if own > NO_MATCH:
        return Eligibility(True, MATCH_NAMES[own], None)
    return Eligibility(False)
