"""
One-off: prints the actual description context around a likely_match for a
specific job, so you can see exactly which phrase triggered it and judge
whether it's a real signal or a false positive.

Run from project root:
    python -m backend.job_scraper.inspect_likely_match
"""
import json
from ..config import DATA_DIR
from ..matching_engine.skill_categories import get_category_phrases
import re


def main():
    with open(DATA_DIR / "jobs.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    targets = ["Researcher, Trustworthy AI", "Machine Learning Engineer II, Computer Vision"]

    for job in jobs:
        title = job.get("title", "")
        if any(t in title for t in targets):
            desc = job.get("clean_description", "") or ""
            for phrase in get_category_phrases("GANs"):
                m = re.search(r'\b' + re.escape(phrase) + r'\b', desc.lower())
                if m:
                    start = max(0, m.start() - 100)
                    end = min(len(desc), m.end() + 100)
                    print("=" * 90)
                    print(f"{title} @ {job.get('company')}")
                    print(f"  matched phrase: '{phrase}'")
                    print(f"  context: ...{desc[start:end]}...")


if __name__ == "__main__":
    main()