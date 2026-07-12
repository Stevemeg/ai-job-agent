"""
One-off sanity check: prints the actual description snippet around the years
match for a sample of jobs gated out by years_required_too_high, so you can
eyeball whether the 54.3% figure is catching genuine senior-role requirements
or false-positives from the regex.

Run from project root:
    python -m backend.job_scraper.sample_years_gate
"""
import json
import random
from ..config import DATA_DIR, HARD_FILTERS
from ..matching_engine.requirement_extractor import extract_requirements, YEARS_PATTERN


def main(sample_size=10):
    with open(DATA_DIR / "jobs.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    gated = []
    for job in jobs:
        desc = job.get("clean_description", "")
        reqs = extract_requirements(desc)
        if (reqs["years_required"] is not None and reqs["years_is_strict"]
                and reqs["years_required"] > HARD_FILTERS["max_required_years_for_fresher"]):
            gated.append((job, reqs, desc))

    print(f"Total gated by years_required_too_high: {len(gated)}")
    sample = random.sample(gated, min(sample_size, len(gated)))

    for job, reqs, desc in sample:
        match = YEARS_PATTERN.search(desc)
        start = max(0, match.start() - 80) if match else 0
        end = min(len(desc), match.end() + 80) if match else 150
        snippet = desc[start:end].replace("\n", " ")
        print("=" * 90)
        print(f"{job.get('title')} @ {job.get('company')}")
        print(f"  extracted: years={reqs['years_required']} strict={reqs['years_is_strict']}")
        print(f"  context:  ...{snippet}...")


if __name__ == "__main__":
    main()