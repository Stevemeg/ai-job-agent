"""
Diagnostic: shows which (company, title) keys had multiple entries BEFORE
dedup ran, so you can sanity-check whether the dedup was correct or too
aggressive. Run this against a BACKUP of the pre-dedup file if you have one;
otherwise it's still useful run against jobs.json now to confirm no
unexpected collisions remain.

Run from project root:
    python -m backend.job_scraper.inspect_dupes
"""
import json
from collections import Counter
from ..config import DATA_DIR


def _job_key(job):
    title = " ".join((job.get("title") or "").lower().split())
    company = (job.get("company") or "").lower().strip()
    return (company, title)


def inspect():
    jobs_path = DATA_DIR / "jobs.json"
    with open(jobs_path, "r", encoding="utf-8") as f:
        jobs = json.load(f)

    counts = Counter(_job_key(j) for j in jobs)
    dupes = {k: v for k, v in counts.items() if v > 1}

    print(f"Total jobs: {len(jobs)}")
    print(f"Unique (company, title) keys: {len(counts)}")
    print(f"Keys appearing more than once: {len(dupes)}")
    print()
    print("Top 15 most-repeated (company, title) pairs still in the file:")
    for (company, title), count in sorted(dupes.items(), key=lambda x: -x[1])[:15]:
        print(f"  {count:3}x | {company:20} | {title}")


if __name__ == "__main__":
    inspect()