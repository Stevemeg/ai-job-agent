"""Explainability: which of the candidate's skills are evidenced in the job
(via tags OR description) vs which seem missing.

Same root cause as the skill_overlap fix in scorer.py: RemoteOK tags are
skill-shaped ("pytorch", "nlp"), so matching against tags alone used to work.
ATS jobs (Greenhouse/Lever/Ashby) tag by DEPARTMENT ("Engineering", "Research")
- there's no skill signal in tags at all, so strong_matches came back empty
for every ATS job regardless of actual fit. Fixed by also checking the
candidate's own skill list against the job description directly.

Phase 4: also checks every alias/synonym of a skill (see skill_taxonomy.py),
so this stays consistent with scorer.skill_overlap_score -- a skill that
counts toward the numeric score now also shows up here as a strong_match,
rather than the two disagreeing with each other.

Phase 4, Mode B: distinguishes EXACT/alias evidence (strong_matches) from
CATEGORY-level evidence (likely_matches) -- a JD mentioning "vision-language
model" is real but weaker evidence for a candidate's "CLIP" skill than the
JD naming CLIP directly. Kept as a separate bucket rather than merged into
strong_matches, so the displayed explanation doesn't overstate confidence.
"""
import re
from .skill_taxonomy import any_surface_form_present
from .skill_categories import get_category_phrases

NOISE_TAGS = {"travel", "growth", "leader", "senior", "junior", "digital nomad",
              "management", "lead", "design", "support", "technical"}


def explain_match(candidate_profile, job):
    """Returns:
      strong_matches  -- skills with EXACT/alias evidence (full confidence)
      likely_matches  -- skills with only CATEGORY-level evidence (partial
                          confidence -- the JD mentions a broader concept the
                          skill is an example of, not the skill itself)
      missing_skills  -- skills with no evidence of either kind
    """
    candidate_skills = [s["skill"] for s in candidate_profile.get("skills", [])]
    if not candidate_skills:
        return {"strong_matches": [], "likely_matches": [], "missing_skills": []}

    tags = [t for t in job.get("tags", []) if t.lower() not in NOISE_TAGS]
    description = job.get("clean_description", "") or ""
    combined_text = (" ".join(tags) + " " + description).lower()

    strong, likely, missing = [], [], []
    for skill in candidate_skills:
        if any_surface_form_present(skill, combined_text):
            strong.append(skill)
        elif any(re.search(r'\b' + re.escape(phrase) + r'\b', combined_text)
                  for phrase in get_category_phrases(skill)):
            likely.append(skill)
        else:
            missing.append(skill)

    return {"strong_matches": strong, "likely_matches": likely, "missing_skills": missing}