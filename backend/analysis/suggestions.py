"""
suggestions.py -- specific, evidence-backed resume suggestions.

NOT generic advice. Every suggestion is triggered by a concrete finding in
THIS resume and quotes the evidence (the actual bullet, the actual count),
so the user sees exactly what to fix. Rules derive from the health-score
findings plus resume-writing heuristics recruiters/ATS actually care about.

Each suggestion: {"title", "detail", "severity", "category"}
severity: "high" | "medium" | "low"
"""
from __future__ import annotations

import re
from typing import Any

from .resume_health import ACTION_VERBS, _split_bullets, _NUM_RE

WEAK_VERBS = {"worked", "helped", "did", "made", "used", "involved",
              "responsible", "participated", "assisted"}

DEPLOYMENT_SIGNALS = ("docker", "kubernetes", "aws", "gcp", "azure", "deploy",
                      "production", "ci/cd", "hosted", "cloud")


def generate_suggestions(profile: dict, health: dict,
                         gaps: list[dict] | None = None) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    projects = profile.get("projects", [])
    all_bullets = [(p.get("title", ""), b) for p in projects
                   for b in _split_bullets(p.get("description", ""))]
    text = " ".join(b for _, b in all_bullets).lower()

    # 1. Unquantified bullets -- quote the worst offenders
    unquantified = [(t, b) for t, b in all_bullets if not _NUM_RE.search(b)]
    if unquantified:
        example = unquantified[0][1]
        suggestions.append({
            "title": f"Quantify {len(unquantified)} bullet(s) that have no metrics",
            "detail": (f'E.g. in "{unquantified[0][0]}": "{example[:120]}..." -- '
                       "add a number: dataset size, accuracy, latency, "
                       "throughput, or % improvement."),
            "severity": "high", "category": "Impact",
        })

    # 2. Weak verbs -- quote them
    weak = [(t, b) for t, b in all_bullets
            if (b.split() or [""])[0].lower().rstrip(",.") in WEAK_VERBS]
    if weak:
        suggestions.append({
            "title": f"Replace weak opening verbs in {len(weak)} bullet(s)",
            "detail": (f'"{weak[0][1][:100]}..." -- start with a strong verb: '
                       "Architected, Engineered, Deployed, Optimized."),
            "severity": "medium", "category": "Language",
        })

    # 3. Deployment story missing
    if not any(s in text for s in DEPLOYMENT_SIGNALS):
        suggestions.append({
            "title": "No deployment/production story detected",
            "detail": ("Every project reads as local/research. If anything ran "
                       "in Docker, on a cloud VM, or served real users -- say so. "
                       "If not, containerizing ONE project (e.g. with Docker) is "
                       "the fastest resume upgrade available."),
            "severity": "high", "category": "Skills",
        })

    # 4. Market gaps (from skill_gap) -- top 3 as one actionable card
    if gaps:
        top = gaps[:3]
        gap_list = ", ".join(f"{g['skill']} (in {g['demand_pct']}% of jobs)"
                             for g in top)
        suggestions.append({
            "title": "Highest-demand skills missing from your resume",
            "detail": (f"{gap_list}. These are computed from your actual job "
                       "corpus, not generic advice. Learn the top one and add a "
                       "project bullet using it."),
            "severity": "high", "category": "Market Fit",
        })

    # 5. Missing links
    for field, label in [("linkedin", "LinkedIn"), ("github", "GitHub")]:
        if not profile.get(field):
            suggestions.append({
                "title": f"Add your {label} URL",
                "detail": f"Recruiters check {label} before replying. Make it a "
                          "clickable hyperlink in the PDF.",
                "severity": "medium", "category": "Contact",
            })

    # 6. Surface remaining health findings not already covered
    covered = {"Quantified Impact", "ATS Readiness"}
    for dim, data in health.get("breakdown", {}).items():
        if dim in covered:
            continue
        for f in data.get("findings", []):
            suggestions.append({
                "title": dim,
                "detail": f,
                "severity": "low", "category": dim,
            })

    order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: order[s["severity"]])
    return suggestions
