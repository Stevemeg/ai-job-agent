"""
titles.py -- the seniority filter triage applies to job titles
(AGENT_EXCLUDE_TITLE_WORDS). Scoring and ranking never look at it; only
the agent's triage does, so a senior job still ranks exactly as before --
the agent just doesn't draft for it.

A title is excluded when it contains one of the words as a WHOLE word,
case-insensitively. Words are split the way locations.py splits them
(letters and digits; punctuation, hyphens and spaces separate words):

  "Senior Staff Software Engineer"  -> senior, staff   (excluded)
  "Sr. ML Engineer"                 -> sr              (excluded)
  "Tech-Lead, Platform"             -> lead            (excluded)
  "Leading-edge AI Engineer"        -> leading, edge   (kept: not "lead")
  "Headless CMS Developer"          -> headless        (kept: not "head")
  "AI Engineer"                     -> nothing         (kept)

An entry of several words ("vice president") matches only as those
consecutive words.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .locations import _contains, words

DEFAULT_EXCLUDE_TITLE_WORDS = ("senior", "sr", "staff", "principal", "lead",
                               "manager", "director", "head", "architect", "vp",
                               "distinguished")


@dataclass(frozen=True)
class TitlePolicy:
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE_TITLE_WORDS

    def __post_init__(self) -> None:
        object.__setattr__(self, "_needles",
                           tuple((e, w) for e, w in ((e, words(e)) for e in self.exclude)
                                 if w))

    def excluded_by(self, title: Any) -> str | None:
        """The first exclude entry the title contains as whole words, or
        None (the title is kept)."""
        w = words(title)
        for entry, needle in self._needles:
            if _contains(w, needle):
                return entry
        return None

    def keeps(self, title: Any) -> bool:
        return self.excluded_by(title) is None
