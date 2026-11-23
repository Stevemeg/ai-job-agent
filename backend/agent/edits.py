"""
edits.py -- a person's edits to an application awaiting approval, applied
and re-validated with the truthfulness validators (no LLM). The decision
API runs it to refuse flagged edits (422 unless acknowledged); the graph's
apply_edit node runs it again on the same checkpointed state, so both see
the same flags.

Edits (JSON):

  {"cover_letter_paragraphs": ["...", "..."],          the whole letter body
   "resume_bullets": {"<project title>": ["...", ...]}} per project

Either key or both. Paragraphs are validated with the cover-letter
validator (as drafts are; company and role words are fine),
bullets with the resume validator against that project's original
description. A flag is
reported only for text the edit changed.

Unlike the model's output, an acknowledged human edit is KEPT as written
(no fallback to the original bullets): the person is the author. Its
flags stay in the validator report and set needs_review (ADR-001: flagged
content is shown, never silently dropped).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

MAX_PARAGRAPHS = 8
MAX_BULLETS = 12
MAX_CHARS = 2000
EDIT_KEYS = ("cover_letter_paragraphs", "resume_bullets")


class InvalidEdit(ValueError):
    """Malformed edits: unknown keys, empty text, an unknown project."""


@dataclass
class EditResult:
    paragraphs: list[str]
    resume_content: dict
    resume_report: list[dict]
    letter_review: list[dict]
    flags: list[dict] = field(default_factory=list)   # in the edited text only
    changed: dict = field(default_factory=dict)


def _texts(value, what: str, limit: int) -> list[str]:
    if not isinstance(value, list) or not value:
        raise InvalidEdit(f"{what} must be a non-empty list of strings")
    if len(value) > limit:
        raise InvalidEdit(f"{what}: at most {limit} entries")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InvalidEdit(f"{what}: every entry must be non-empty text")
        if len(item) > MAX_CHARS:
            raise InvalidEdit(f"{what}: an entry is longer than {MAX_CHARS} characters")
        out.append(" ".join(item.split()))
    return out


def normalize(edits) -> dict:
    """The edits, validated for shape (InvalidEdit otherwise)."""
    if not isinstance(edits, dict) or not edits:
        raise InvalidEdit("edits must name cover_letter_paragraphs and/or resume_bullets")
    unknown = sorted(set(edits) - set(EDIT_KEYS))
    if unknown:
        raise InvalidEdit(f"unknown edit keys: {', '.join(unknown)}")
    out = {}
    if "cover_letter_paragraphs" in edits:
        out["cover_letter_paragraphs"] = _texts(edits["cover_letter_paragraphs"],
                                                "cover_letter_paragraphs", MAX_PARAGRAPHS)
    if "resume_bullets" in edits:
        bullets = edits["resume_bullets"]
        if not isinstance(bullets, dict) or not bullets:
            raise InvalidEdit("resume_bullets must map a project title to its bullets")
        out["resume_bullets"] = {str(title): _texts(b, f"resume_bullets[{title!r}]",
                                                    MAX_BULLETS)
                                 for title, b in bullets.items()}
    return out


def _key(title) -> str:
    return " ".join(str(title or "").split()).lower()


def apply_edits(state: dict, edits) -> EditResult:
    """Apply `edits` to a drafted application's checkpointed state
    (profile, job, paragraphs, resume_content, resume_report,
    letter_review) and re-validate the edited text."""
    from ..resume_engine import cover_letter
    from ..resume_engine.validate_tailored import validate_project

    edits = normalize(edits)
    profile, job = state["profile"], state["job"]
    paragraphs = list(state["paragraphs"])
    content = copy.deepcopy(state["resume_content"])
    report = copy.deepcopy(state["resume_report"])
    review = list(state.get("letter_review") or [])
    flags, changed = [], {}

    if "cover_letter_paragraphs" in edits:
        new = edits["cover_letter_paragraphs"]
        old = [" ".join(p.split()) for p in paragraphs]
        edited = [i for i, p in enumerate(new, 1) if i > len(old) or p != old[i - 1]]
        review = cover_letter.letter_review(new, profile, job)
        flags += [{"document": "cover_letter", "paragraph": r["paragraph"],
                   "flags": r["flags"]} for r in review if r["paragraph"] in edited]
        if new != old:
            changed["cover_letter_paragraphs"] = edited
        paragraphs = new

    if "resume_bullets" in edits:
        skills = [s["skill"] for s in profile.get("skills", [])]
        originals = {_key(p["title"]): p for p in profile.get("projects", [])}
        rendered = {_key(p["title"]): p for p in content.get("projects", [])}
        changed["resume_projects"] = []
        for title, bullets in edits["resume_bullets"].items():
            source, target = originals.get(_key(title)), rendered.get(_key(title))
            if source is None or target is None:
                raise InvalidEdit(f"no project titled {title!r} in this resume")
            if bullets == [" ".join(b.split()) for b in target["bullets"]]:
                continue                                    # unchanged
            result = validate_project(bullets, source["description"], skills)
            flagged = sorted({f for b in result["bullets"]
                              for f in b["flagged_terms"] + b["flagged_numbers"]})
            target["bullets"] = bullets
            entry = {"project": source["title"], "ok": result["ok"],
                     "flagged": flagged, "edited": True}
            report = [entry if _key(r.get("project")) == _key(source["title"]) else r
                      for r in report]
            if flagged:
                flags.append({"document": "resume", "project": source["title"],
                              "flags": flagged})
            changed["resume_projects"].append(source["title"])

    if "cover_letter_paragraphs" not in changed and not changed.get("resume_projects"):
        raise InvalidEdit("the edits change nothing")
    return EditResult(paragraphs=paragraphs, resume_content=content,
                      resume_report=report, letter_review=review,
                      flags=flags, changed=changed)
