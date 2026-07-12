"""
One-off diagnostic: confirms likely_matches (Mode B category matching) is
actually firing somewhere in the real dataset, not just silently empty
everywhere. Prints a few real examples if found.

Run from project root:
    python -m backend.job_scraper.check_likely_matches
"""
import json
from ..config import DATA_DIR, PROFILE_FILE
from ..matching_engine.matcher import rank_jobs


def main():
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        profile = json.load(f)
    with open(DATA_DIR / "jobs.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    ranked = rank_jobs(profile, jobs, candidate_is_fresher=True)

    with_likely = [r for r in ranked if r.get("likely_matches")]
    print(f"Jobs with at least one likely_match (category-level evidence): {len(with_likely)} / {len(ranked)}")
    print()
    for r in with_likely[:8]:
        print(f"{r['match_score']:5.1f} | {r['title'][:45]:45} | {r['company']}")
        print(f"       likely_matches: {r['likely_matches']}")
        print()


if __name__ == "__main__":
    main()