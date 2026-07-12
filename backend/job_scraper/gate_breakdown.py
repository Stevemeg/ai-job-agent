"""
Diagnostic: breaks down exactly WHICH hard gate is responsible for removing
each job, so you can see "40% of removed jobs were the sponsorship gate" vs
"this is mostly junk titles" instead of only seeing one aggregate number.

Doesn't change any ranking behavior -- read-only diagnostic.

Run from project root:
    python -m backend.job_scraper.gate_breakdown
"""
import json
from collections import Counter
from ..config import DATA_DIR, HARD_FILTERS
from ..matching_engine.requirement_extractor import extract_requirements


def _title_gate_fail(job):
    title = (job.get("title") or "").lower()
    return any(bad in title for bad in HARD_FILTERS["drop_titles_containing"])


def classify_gate_failure(job, candidate_is_fresher=True):
    """Returns the FIRST gate reason a job fails, or None if it passes.
    Mirrors the exact order scorer.passes_hard_filters checks, so the
    counts here add up to the real removed-job total."""
    if _title_gate_fail(job):
        return "title_keyword"

    reqs = extract_requirements(job.get("clean_description", ""))

    if candidate_is_fresher:
        if (reqs["years_required"] is not None
                and reqs["years_is_strict"]
                and reqs["years_required"] > HARD_FILTERS["max_required_years_for_fresher"]):
            return "years_required_too_high"

    if reqs["requires_clearance"]:
        return "security_clearance"

    if reqs["no_sponsorship"] and HARD_FILTERS.get("candidate_needs_sponsorship", False):
        return "no_sponsorship"

    return None  # passes all gates


def main():
    with open(DATA_DIR / "jobs.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    # Mirror matcher.py's dedup so counts reflect what rank_jobs actually sees
    seen = set()
    deduped = []
    for job in jobs:
        title = " ".join((job.get("title") or "").lower().split())
        company = (job.get("company") or "").lower().strip()
        key = (company, title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)

    print(f"Total jobs (raw): {len(jobs)}")
    print(f"After dedup: {len(deduped)}")
    print()

    reasons = Counter()
    for job in deduped:
        reason = classify_gate_failure(job, candidate_is_fresher=True)
        reasons[reason or "PASSES"] += 1

    print("=" * 60)
    print("GATE BREAKDOWN (first failing gate per job, dedup'd):")
    print("=" * 60)
    total = len(deduped)
    for reason, count in reasons.most_common():
        pct = 100 * count / total
        print(f"  {count:5} ({pct:5.1f}%)  {reason}")


if __name__ == "__main__":
    main()