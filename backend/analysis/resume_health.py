"""
resume_health.py -- deterministic 0-100 resume health score.

WHY DETERMINISTIC: an LLM grader gives a different number every run, costs
tokens, and can't explain itself honestly. This scorer is instant, free,
identical on every run, and every point is traceable to a concrete finding
the UI can show the user.

Six dimensions, weights sum to 100:
    contact      15  -- name/email/phone/linkedin/github present
    skills       20  -- breadth + category diversity
    projects     20  -- count, real titles, enough bullets
    quantified   15  -- share of bullets containing numbers/%/metrics
    education    10  -- degree + institution + years present
    ats_ready    20  -- action verbs, bullet lengths, section completeness
"""
from __future__ import annotations

import re
from typing import Any

ACTION_VERBS = {
    "architected", "built", "created", "designed", "developed", "deployed",
    "engineered", "implemented", "integrated", "led", "optimized", "achieved",
    "delivered", "generated", "evaluated", "automated", "improved", "reduced",
    "increased", "trained", "launched", "streamlined", "spearheaded",
    "constructed", "established", "produced",
}

_NUM_RE = re.compile(r"\d+(?:[,.]\d+)?\s*[%x×+]?|\bsub-second\b", re.IGNORECASE)


def _split_bullets(description: str) -> list[str]:
    return [b.strip() for b in re.split("•", description or "") if b.strip()]


def _grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "E"


def _score_contact(profile: dict) -> tuple[float, list[str]]:
    findings = []
    pts = 0.0
    for field, weight, label in [("name", 3, "Name"), ("email", 4, "Email"),
                                 ("phone", 3, "Phone"), ("linkedin", 3, "LinkedIn"),
                                 ("github", 2, "GitHub")]:
        if profile.get(field):
            pts += weight
        else:
            findings.append(f"{label} missing -- ATS and recruiters expect it.")
    return pts, findings


def _score_skills(profile: dict) -> tuple[float, list[str]]:
    skills = profile.get("skills", [])
    findings = []
    count_pts = min(len(skills), 15) / 15 * 12          # up to 12 pts for breadth
    categories = {s.get("category", "") for s in skills}
    diversity_pts = min(len(categories), 8) / 8 * 8      # up to 8 pts for diversity
    if len(skills) < 10:
        findings.append(f"Only {len(skills)} skills detected -- aim for 12-20 "
                        "specific, real skills.")
    if len(categories) < 5:
        findings.append("Skills cluster in few categories -- add breadth "
                        "(e.g. deployment, cloud, databases) if you have it.")
    return count_pts + diversity_pts, findings


def _score_projects(profile: dict) -> tuple[float, list[str]]:
    projects = profile.get("projects", [])
    findings = []
    pts = min(len(projects), 3) / 3 * 8                  # up to 8 pts for count
    titled = sum(1 for p in projects if (p.get("title") or "").strip())
    if projects and titled < len(projects):
        findings.append("Some projects have empty titles -- parser or resume issue.")
    pts += (titled / len(projects) * 4) if projects else 0
    bullets_ok = sum(1 for p in projects if len(_split_bullets(p.get("description", ""))) >= 3)
    pts += (bullets_ok / len(projects) * 8) if projects else 0
    if projects and bullets_ok < len(projects):
        findings.append("Aim for 3-4 bullets per project; some have fewer.")
    if not projects:
        findings.append("No projects detected -- for a new grad, projects ARE "
                        "the experience section.")
    return pts, findings


def _score_quantified(profile: dict) -> tuple[float, list[str]]:
    all_bullets = [b for p in profile.get("projects", [])
                   for b in _split_bullets(p.get("description", ""))]
    if not all_bullets:
        return 0.0, ["No bullets to evaluate."]
    quantified = sum(1 for b in all_bullets if _NUM_RE.search(b))
    ratio = quantified / len(all_bullets)
    findings = []
    if ratio < 0.6:
        findings.append(f"Only {quantified}/{len(all_bullets)} bullets contain "
                        "numbers. Add metrics: accuracy, latency, dataset size, "
                        "throughput, % improvement.")
    return ratio * 15, findings


def _score_education(profile: dict) -> tuple[float, list[str]]:
    edu = profile.get("education", [])
    if not edu:
        return 0.0, ["No education section detected."]
    e = edu[0]
    pts, findings = 0.0, []
    if e.get("degree"):
        pts += 4
    else:
        findings.append("Degree name missing.")
    if e.get("college"):
        pts += 3
    else:
        findings.append("Institution missing.")
    if e.get("years"):
        pts += 2
    if e.get("cgpa"):
        pts += 1
    return pts, findings


def _score_ats_ready(profile: dict) -> tuple[float, list[str]]:
    findings = []
    all_bullets = [b for p in profile.get("projects", [])
                   for b in _split_bullets(p.get("description", ""))]
    if not all_bullets:
        return 0.0, ["No content to check for ATS-readiness."]

    verb_starts = sum(1 for b in all_bullets
                      if (b.split() or [""])[0].lower().rstrip(",.") in ACTION_VERBS)
    verb_ratio = verb_starts / len(all_bullets)
    if verb_ratio < 0.8:
        findings.append(f"{len(all_bullets) - verb_starts} bullets don't start "
                        "with a strong action verb.")

    good_len = sum(1 for b in all_bullets if 8 <= len(b.split()) <= 40)
    len_ratio = good_len / len(all_bullets)
    if len_ratio < 0.8:
        findings.append("Some bullets are too short (<8 words) or too long "
                        "(>40 words) for ATS/recruiter scanning.")

    # Section completeness: skills + education + projects all present
    sections = sum(bool(profile.get(k)) for k in ("skills", "education", "projects"))
    section_pts = sections / 3 * 6

    return verb_ratio * 8 + len_ratio * 6 + section_pts, findings


def compute_health(profile: dict) -> dict[str, Any]:
    """Returns {"score": float, "grade": str, "breakdown": {dim: {...}}}."""
    dims = {
        "Contact & Links": (_score_contact, 15),
        "Skills": (_score_skills, 20),
        "Projects": (_score_projects, 20),
        "Quantified Impact": (_score_quantified, 15),
        "Education": (_score_education, 10),
        "ATS Readiness": (_score_ats_ready, 20),
    }
    breakdown = {}
    total = 0.0
    for name, (fn, max_pts) in dims.items():
        pts, findings = fn(profile)
        pts = min(pts, max_pts)
        total += pts
        breakdown[name] = {"score": round(pts, 1), "max": max_pts,
                           "findings": findings}
    total = round(total, 1)
    return {"score": total, "grade": _grade(total), "breakdown": breakdown}
