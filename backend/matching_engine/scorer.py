"""
Structured scoring = hard filters (gates) + weighted soft score.

Why this beats the old pure-embedding ranking:
  - Resume-vs-JD cosine similarity rewards vocabulary overlap, not fit, and
    produced weak, bunched scores (top match was only 38.7%).
  - Disqualifiers (years required, sales roles) must REMOVE a job, not nudge
    its rank. Blending them into one number produces confidently-wrong results.

The final score is an interpretable blend you can show the user AND defend in
an interview: "0.82 = 0.40*skill + 0.30*semantic + 0.20*role + 0.10*seniority".
"""
import re
from sklearn.metrics.pairwise import cosine_similarity

from ..config import WEIGHTS, HARD_FILTERS
from ..embeddings import get_model
from .role_scorer import compute_role_score   # reused, normalized below
from .requirement_extractor import extract_requirements
from .skill_taxonomy import any_surface_form_present
from .skill_categories import get_category_phrases, CATEGORY_MATCH_CREDIT


# ----------------------------- hard gates -----------------------------------
def passes_hard_filters(job, candidate_is_fresher=True):
    title = (job.get("title") or "").lower()
    if any(bad in title for bad in HARD_FILTERS["drop_titles_containing"]):
        return False

    description = job.get("clean_description", "")
    reqs = extract_requirements(description)

    if candidate_is_fresher:
        # Only gate on a STRICT years requirement. "3-5 years preferred" no
        # longer disqualifies a fresher the way it incorrectly did before --
        # only language like "5+ years required" or a bare number with no
        # softening signal does. See requirement_extractor.py for the logic.
        if (reqs["years_required"] is not None
                and reqs["years_is_strict"]
                and reqs["years_required"] > HARD_FILTERS["max_required_years_for_fresher"]):
            return False

    # Clearance and no-sponsorship gates apply regardless of seniority --
    # these are hard eligibility walls, not experience-level judgments.
    if reqs["requires_clearance"]:
        return False
    if reqs["no_sponsorship"] and HARD_FILTERS.get("candidate_needs_sponsorship", False):
        return False

    return True


# ----------------------------- soft sub-scores ------------------------------
def _tokens(s):
    return set(re.findall(r"[a-z0-9+#.]+", s.lower()))


def skill_overlap_score(candidate_skills, job_tags, job_description=""):
    """Weighted fraction of candidate skills evidenced in the job's tags OR
    description (0..1). Each skill contributes:
      - 1.0 if an exact name or alias/synonym is found (see skill_taxonomy.py)
      - CATEGORY_MATCH_CREDIT (0.6) if only a broader CATEGORY phrase is
        found -- e.g. a JD saying "vision-language model" is real but weaker
        evidence for a candidate who lists "CLIP" specifically (see
        skill_categories.py)
      - 0.0 otherwise

    RemoteOK jobs have rich skill tags (e.g. "pytorch", "nlp") so tags alone
    used to be enough. ATS jobs (Greenhouse/Lever/Ashby) tag by DEPARTMENT
    ("Engineering", "Data") instead — the real skill signal lives in the
    description text. Without checking description too, every ATS job would
    show skill_overlap=0 regardless of actual fit, silently breaking the one
    metric Phase 1 (ATS sourcing) was meant to fix.

    Phase 4 addition: checks every ALIAS/synonym of a skill, not just its
    literal name. "Retrieval-Augmented Generation" in a JD now counts as
    evidence of "RAG" on the candidate's profile, and vice versa -- confirmed
    via this candidate's own skill list containing both forms already.
    """
    if not candidate_skills:
        return 0.0

    tags_text = " ".join(job_tags).lower()
    desc_text = (job_description or "").lower()
    combined_text = tags_text + " " + desc_text

    credit_total = 0.0
    for s in candidate_skills:
        if any_surface_form_present(s, combined_text):
            credit_total += 1.0
        elif any(re.search(r'\b' + re.escape(phrase) + r'\b', combined_text)
                 for phrase in get_category_phrases(s)):
            credit_total += CATEGORY_MATCH_CREDIT
    return min(credit_total / max(len(candidate_skills), 1), 1.0)


def semantic_score(candidate_text, job_text):
    model = get_model()
    emb = model.encode([candidate_text, job_text])
    return float(cosine_similarity([emb[0]], [emb[1]])[0][0])


def role_score_normalized(job):
    # compute_role_score returns roughly -20..+20; squash to 0..1.
    raw = compute_role_score(job)
    return max(0.0, min((raw + 10) / 30.0, 1.0))


def seniority_score(job, candidate_is_fresher=True):
    sen = (job.get("seniority") or "Mid-Level").lower()
    if candidate_is_fresher:
        return {"intern": 1.0, "junior": 1.0, "mid-level": 0.6,
                "lead": 0.2, "senior": 0.1,
                "staff": 0.05, "principal": 0.05,
                "phd": 0.05}.get(sen, 0.5)  # PhD enrollment is a hard gate most freshers don't meet
    return {"senior": 1.0, "lead": 1.0, "staff": 1.0, "principal": 0.9,
            "phd": 0.7, "mid-level": 0.7, "junior": 0.3, "intern": 0.1}.get(sen, 0.5)


# ----------------------------- combine --------------------------------------
def score_job(candidate_skills, candidate_text, job, candidate_is_fresher=True):
    job_tags = job.get("tags", []) or []
    job_text = "Title: %s Tags: %s Description: %s" % (
        job.get("title", ""), " ".join(job_tags), job.get("clean_description", ""))

    subs = {
        "skill_overlap": skill_overlap_score(
            candidate_skills, job_tags, job.get("clean_description", "")),
        "semantic":      semantic_score(candidate_text, job_text),
        "role":          role_score_normalized(job),
        "seniority":     seniority_score(job, candidate_is_fresher),
    }
    final = sum(WEIGHTS[k] * subs[k] for k in WEIGHTS)
    return round(final * 100, 2), {k: round(v, 3) for k, v in subs.items()}