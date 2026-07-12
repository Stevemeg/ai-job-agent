"""
Central configuration. Import paths/weights from here instead of hardcoding
'../data' everywhere — this is what lets the pipeline run from any directory
and what an interviewer expects to see in a multi-file project.
"""
from pathlib import Path

# Project root resolved relative to THIS file, so paths work no matter where
# you launch python from (the old '../data' only worked from inside backend/).
ROOT = Path(__file__).resolve().parents[1]          # AI_JOB_AGENT/
BACKEND = ROOT / "backend"

DATA_DIR = ROOT / "data"
UPLOADS_DIR = ROOT / "uploads"
DATASETS_DIR = ROOT / "datasets"

SKILLS_DB = DATASETS_DIR / "skills_database.csv"
JOBS_FILE = DATA_DIR / "jobs.json"
PROFILE_FILE = DATA_DIR / "candidate_profile.json"
RANKED_FILE = DATA_DIR / "ranked_jobs.json"

EMBED_MODEL = "all-MiniLM-L6-v2"

# --- Scoring weights (must sum to 1.0). These are tunable; once you have a
# --- feedback/events log you replace these hand-set weights with learned ones.
WEIGHTS = {
    "skill_overlap": 0.40,   # explicit skill/tag intersection — most defensible
    "semantic":      0.30,   # embedding similarity — captures phrasing the keywords miss
    "role":          0.20,   # title relevance to the candidate's track
    "seniority":     0.10,   # fresher vs senior alignment
}

# Hard gates: a job failing any TRUE gate is removed, never just down-ranked.
HARD_FILTERS = {
    "drop_titles_containing": [
        "sales", "account manager", "recruiter", "marketing", "seo",
        "customer support", "project manager", "product manager",
    ],
    # If the candidate is a fresher/new-grad, drop roles demanding many years.
    "max_required_years_for_fresher": 3,
    # Set True if the candidate would need visa sponsorship for the roles
    # being searched (e.g. applying to US-based companies from outside the
    # US). When True, jobs explicitly stating no sponsorship / US-citizens-
    # only are hard-gated out rather than wasting a ranked slot on them.
    "candidate_needs_sponsorship": True,
}