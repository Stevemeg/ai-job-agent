"""
One-time fix-up: re-runs the corrected detect_seniority() over every job
already saved in data/jobs.json.

Why this is needed: fixing job_parser.py only affects jobs fetched AFTER the
fix. Jobs already on disk (from earlier ats_collector.py runs) still have the
OLD seniority tag baked in -- e.g. "Principal Research Scientist" saved as
"Mid-Level" before detect_seniority() recognized "principal" at all. Re-run
this once after updating job_parser.py to correct existing data in place.

Run from project root:
    python -m backend.job_scraper.retag_seniority
"""
import json
from collections import Counter
from .job_parser import detect_seniority
from ..config import DATA_DIR


def retag():
    jobs_path = DATA_DIR / "jobs.json"
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    before = Counter(j.get("seniority") for j in jobs)
    changed = 0
    for job in jobs:
        new_sen = detect_seniority(job.get("title") or "")
        if new_sen != job.get("seniority"):
            job["seniority"] = new_sen
            changed += 1
    after = Counter(j.get("seniority") for j in jobs)

    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=4, ensure_ascii=False)

    print(f"Re-tagged {changed} / {len(jobs)} jobs with corrected seniority")
    print(f"\nBefore: {dict(before)}")
    print(f"After:  {dict(after)}")


if __name__ == "__main__":
    retag()
    