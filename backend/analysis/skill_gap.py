"""
skill_gap.py -- market-driven skill gap analysis.

WHY THIS IS HONEST: "impact" numbers on career sites are invented. Here,
the impact of a missing skill is computed from the user's OWN job corpus:
the share of relevant jobs whose description mentions that skill. If 41%
of the 3,875 collected jobs mention Docker and the candidate doesn't have
it, that's a real, defensible number -- not marketing.

MARKET_SKILLS is a curated list of transferable, high-frequency skills a
JD would name explicitly. Domain-agnostic by design: extend the list, not
the logic, to support new verticals.
"""
from __future__ import annotations

import re
from typing import Any

# skill -> regex pattern (word-boundary, case-insensitive)
MARKET_SKILLS: dict[str, str] = {
    "Docker": r"\bdocker\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "AWS": r"\baws\b|\bamazon web services\b",
    "GCP": r"\bgcp\b|\bgoogle cloud\b",
    "Azure": r"\bazure\b",
    "SQL": r"\bsql\b",
    "PostgreSQL": r"\bpostgres(?:ql)?\b",
    "PyTorch": r"\bpytorch\b",
    "TensorFlow": r"\btensorflow\b",
    "Spark": r"\b(?:apache )?spark\b",
    "Airflow": r"\bairflow\b",
    "MLOps": r"\bml ?ops\b",
    "FastAPI": r"\bfastapi\b",
    "REST APIs": r"\brest(?:ful)? api",
    "CI/CD": r"\bci/?cd\b|\bcontinuous integration\b",
    "LangChain": r"\blangchain\b",
    "RAG": r"\brag\b|\bretrieval[- ]augmented generation\b",
    "LLMs": r"\bllms?\b|\blarge language models?\b",
    "Vector Databases": r"\bvector (?:database|db|store|search)\b|\bpinecone\b|\bweaviate\b",
    "Kafka": r"\bkafka\b",
    "Terraform": r"\bterraform\b",
    "Git": r"\bgit\b",
    "Linux": r"\blinux\b",
    "Java": r"\bjava\b",
    # "Go" only via unambiguous forms -- \bgo\b matches the English verb and
    # produced ~26% fake demand in testing. Undercounting beats lying.
    "Go": r"\bgolang\b|\bgo (?:developer|engineer|programming)\b",
    "TypeScript": r"\btypescript\b",
    "React": r"\breact\b",
    "GraphQL": r"\bgraphql\b",
    "Redis": r"\bredis\b",
    "Hugging Face": r"\bhugging ?face\b",
}


def _candidate_has(skill: str, candidate_skills_lower: set[str]) -> bool:
    s = skill.lower()
    if s in candidate_skills_lower:
        return True
    # loose containment: "LLMs" vs "LLM", "Hugging Face" vs "HuggingFace"
    compact = s.replace(" ", "")
    return any(compact == c.replace(" ", "") or s.rstrip("s") == c.rstrip("s")
               for c in candidate_skills_lower)


def compute_skill_gaps(profile: dict, jobs: list[dict],
                       target_roles: list[str] | None = None,
                       top_n: int = 12) -> list[dict[str, Any]]:
    """Returns missing skills, weighted by demand WITHIN the candidate's
    target roles -- 12% overall demand can hide 85% demand among the jobs
    that actually matter to this candidate.

    target_roles: archetype names from career_recommender (their top fits).
    Each item: {"skill", "demand_pct" (overall), "role_demand_pct",
                "jobs_mentioning", "role_jobs", "priority"}
    Priority is driven by role_demand_pct when a meaningful role subset
    exists (>=30 jobs), else falls back to overall demand.
    """
    if not jobs:
        return []
    candidate_skills = {s["skill"].lower() for s in profile.get("skills", [])}

    texts, titles = [], []
    for j in jobs:
        blob = " ".join([
            j.get("title") or "",
            j.get("clean_description") or j.get("description") or "",
            " ".join(j.get("tags") or []),
        ]).lower()
        texts.append(blob)
        titles.append((j.get("title") or "").lower())

    # Subset of jobs whose TITLE matches the candidate's target roles
    role_idx: list[int] = []
    if target_roles:
        from .career_recommender import ROLE_TITLE_KEYWORDS
        keywords = [kw for r in target_roles
                    for kw in ROLE_TITLE_KEYWORDS.get(r, [])]
        role_idx = [i for i, t in enumerate(titles)
                    if any(kw in t for kw in keywords)]
    use_role_subset = len(role_idx) >= 30    # too small = noisy percentages

    gaps = []
    for skill, pattern in MARKET_SKILLS.items():
        if _candidate_has(skill, candidate_skills):
            continue
        rx = re.compile(pattern, re.IGNORECASE)
        hits = sum(1 for t in texts if rx.search(t))
        if hits == 0:
            continue
        pct = hits / len(texts) * 100

        role_hits = sum(1 for i in role_idx if rx.search(texts[i]))
        role_pct = (role_hits / len(role_idx) * 100) if role_idx else 0.0

        ranking_pct = role_pct if use_role_subset else pct
        priority = ("High" if ranking_pct >= 25
                    else "Medium" if ranking_pct >= 10 else "Low")
        gaps.append({
            "skill": skill,
            "demand_pct": round(pct, 1),
            "role_demand_pct": round(role_pct, 1),
            "jobs_mentioning": hits,
            "role_jobs": len(role_idx),
            # True only when the role subset is big enough to trust -- the UI
            # must not headline percentages computed from a handful of jobs.
            "role_weighted": use_role_subset,
            "priority": priority,
            "_rank": ranking_pct,
        })

    gaps.sort(key=lambda g: g["_rank"], reverse=True)
    for g in gaps:
        del g["_rank"]
    return gaps[:top_n]
