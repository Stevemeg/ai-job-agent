"""
One-time cleanup utility: removes exact-duplicate entries from data/jobs.json
that resulted from running ats_collector.py twice before the Lever fix.

Canonical key = (company, normalized title) — same logic matcher.py already
uses internally for ranking-time dedup. This script does it once, on disk,
so the file itself is clean rather than relying on dedup happening silently
at every scoring run.

Run from project root:
    python -m backend.job_scraper.dedupe_jobs
"""
import json
from ..config import DATA_DIR


def _job_key(job):
    title = " ".join((job.get("title") or "").lower().split())
    company = (job.get("company") or "").lower().strip()
    return (company, title)


def dedupe_jobs_file():
    jobs_path = DATA_DIR / "jobs.json"
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    seen = set()
    unique = []
    for job in jobs:
        key = _job_key(job)
        if key in seen:
            continue
        seen.add(key)
        unique.append(job)

    removed = len(jobs) - len(unique)
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(unique, f, indent=4, ensure_ascii=False)

    print(f"Before: {len(jobs)} jobs")
    print(f"After:  {len(unique)} jobs")
    print(f"Removed {removed} exact duplicates (same company + same title)")


if __name__ == "__main__":
    dedupe_jobs_file()