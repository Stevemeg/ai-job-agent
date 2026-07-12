"""
parse_confidence.py -- how much should the user trust the parser's output?

The parser is imperfect (empty project titles, merged descriptions have
happened before). Rather than silently trusting it, every section gets a
confidence % from validity heuristics, plus concrete warnings pointing at
the exact suspect item. The Review & Edit step renders these so the user
knows WHERE to look before analysis runs.

Confidence is about EXTRACTION quality, not resume quality -- a great
resume parsed badly scores low here and high on health, and vice versa.
"""
from __future__ import annotations

import re
from typing import Any

_EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$")
_TITLE_JUNK_RE = re.compile(r"\bLINK\b|http|•")


def _split_bullets(description: str) -> list[str]:
    return [b.strip() for b in re.split("•", description or "") if b.strip()]


def _contact_confidence(profile: dict) -> tuple[float, list[str]]:
    warnings = []
    pts, total = 0.0, 0.0

    name = (profile.get("name") or "").strip()
    total += 30
    if len(name.split()) >= 2 and not any(c.isdigit() for c in name):
        pts += 30
    elif name:
        pts += 15
        warnings.append(f'Name parsed as "{name}" -- please verify.')
    else:
        warnings.append("Name was not detected.")

    email = (profile.get("email") or "").strip()
    total += 40
    if _EMAIL_RE.match(email):
        pts += 40
    elif email:
        pts += 15
        warnings.append(f'Email parsed as "{email}" -- looks malformed.')
    else:
        warnings.append("Email was not detected.")

    phone = re.sub(r"\D", "", profile.get("phone") or "")
    total += 30
    if 10 <= len(phone) <= 13:
        pts += 30
    elif phone:
        pts += 15
        warnings.append("Phone number looks incomplete -- please verify.")
    else:
        warnings.append("Phone was not detected.")

    return pts / total * 100, warnings


def _skills_confidence(profile: dict) -> tuple[float, list[str]]:
    skills = profile.get("skills", [])
    warnings = []
    if not skills:
        return 0.0, ["No skills detected -- the skills database may not cover "
                     "your field, or the PDF text extraction failed."]
    conf = min(len(skills) / 12, 1.0) * 100
    if len(skills) < 8:
        warnings.append(f"Only {len(skills)} skills detected. If your resume "
                        "lists more, add them in the editor.")
    return conf, warnings


def _projects_confidence(profile: dict) -> tuple[float, list[str]]:
    projects = profile.get("projects", [])
    warnings = []
    if not projects:
        return 0.0, ["No projects detected."]
    scores = []
    for i, p in enumerate(projects, 1):
        title = (p.get("title") or "").strip()
        bullets = _split_bullets(p.get("description", ""))
        s = 0.0
        if title and len(title) < 90 and not _TITLE_JUNK_RE.search(title):
            s += 0.45
        else:
            warnings.append(f"Project {i}: title looks wrong or empty "
                            f'("{title[:60]}").')
        if len(bullets) >= 2:
            s += 0.35
        else:
            warnings.append(f'Project {i} ("{title[:40]}"): only '
                            f"{len(bullets)} bullet(s) extracted -- some may "
                            "have been merged or lost.")
        if p.get("duration"):
            s += 0.20
        # Cross-contamination check: next project's title inside this description
        tail = (p.get("description") or "")[-120:]
        if i < len(projects):
            nxt = (projects[i].get("title") or "").strip()
            if nxt and nxt.lower() in tail.lower():
                warnings.append(f"Project {i} description seems to bleed into "
                                f'"{nxt[:40]}" -- boundaries may be wrong.')
                s -= 0.15
        scores.append(max(s, 0.0))
    return sum(scores) / len(scores) * 100, warnings


def _education_confidence(profile: dict) -> tuple[float, list[str]]:
    edu = profile.get("education", [])
    if not edu:
        return 0.0, ["No education detected."]
    e = edu[0]
    present = sum(bool(e.get(k)) for k in ("degree", "college", "years", "cgpa"))
    warnings = []
    if not e.get("degree"):
        warnings.append("Degree name missing from education.")
    if not e.get("college"):
        warnings.append("Institution missing from education.")
    return present / 4 * 100, warnings


def compute_parse_confidence(profile: dict) -> dict[str, Any]:
    """Returns {"overall": %, "sections": {name: {"confidence": %, "warnings": []}}}"""
    sections = {
        "Contact": _contact_confidence(profile),
        "Skills": _skills_confidence(profile),
        "Projects": _projects_confidence(profile),
        "Education": _education_confidence(profile),
    }
    out = {name: {"confidence": round(conf, 0), "warnings": warns}
           for name, (conf, warns) in sections.items()}
    overall = sum(s["confidence"] for s in out.values()) / len(out)
    return {"overall": round(overall, 0), "sections": out}
