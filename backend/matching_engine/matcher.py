"""
Ranking pipeline: dedup -> hard filter -> structured score -> sort.
Drop-in replacement for the old matcher.py (which did pure embedding cosine
and produced weak, bunched scores).
"""
from .explainer import explain_match
from .scorer import score_job, passes_hard_filters


def build_candidate_text(profile):
    skills = " ".join(s["skill"] for s in profile.get("skills", []))
    projects = " ".join(p.get("title", "") + " " + p.get("description", "")
                        for p in profile.get("projects", []))
    education = " ".join(e.get("degree", "") for e in profile.get("education", []))
    return "Skills: %s Projects: %s Education: %s" % (skills, projects, education)


def _job_key(job):
    """Canonical key for dedup: company + normalized title."""
    title = (job.get("title") or "").lower().strip()
    title = " ".join(title.split())
    company = (job.get("company") or "").lower().strip()
    return (company, title)


def rank_jobs(candidate_profile, jobs, candidate_is_fresher=True):
    candidate_skills = [s["skill"] for s in candidate_profile.get("skills", [])]
    candidate_text = build_candidate_text(candidate_profile)

    seen = set()
    ranked = []
    for job in jobs:
        key = _job_key(job)
        if key in seen:                       # dedup
            continue
        seen.add(key)

        if not passes_hard_filters(job, candidate_is_fresher):   # gates
            continue

        score, breakdown = score_job(
            candidate_skills, candidate_text, job, candidate_is_fresher)
        explanation = explain_match(candidate_profile, job)

        ranked.append({
            "title": job.get("title"),
            "company": job.get("company"),
            "location": job.get("location"),
            "match_score": score,
            "score_breakdown": breakdown,          # transparent sub-scores
            "strong_matches": explanation["strong_matches"],
            "likely_matches": explanation["likely_matches"],
            "missing_skills": explanation["missing_skills"],
            "apply_link": job.get("apply_link"),
        })

    return sorted(ranked, key=lambda x: x["match_score"], reverse=True)