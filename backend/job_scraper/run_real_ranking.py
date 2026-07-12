"""
One-off check: rank the REAL candidate_profile.json against the REAL,
deduplicated jobs.json (3,875 jobs from RemoteOK + Greenhouse + Ashby) and
print the top 10 matches with full score breakdowns.

This is the actual test of whether Phase 1 (ATS sourcing) fixed the
skill_overlap=0.0 problem we found with RemoteOK alone.

Phase 2 addition: if Postgres is running (see backend/db/docker-compose.yml),
every job in the ranked list gets auto-logged as an 'impression' event. If
the DB isn't running, ranking still works exactly as before -- logging is
opt-in via --log and fails gracefully with a clear message rather than
crashing the whole script. This matters because Phase 1 didn't require any
infrastructure; Phase 2 shouldn't silently force a dependency onto a script
that worked fine without one.

Run from project root:
    python -m backend.job_scraper.run_real_ranking            # ranking only, no DB needed
    python -m backend.job_scraper.run_real_ranking --log      # also logs impressions to Postgres
"""
import sys
import json
from ..config import DATA_DIR, PROFILE_FILE, RANKED_FILE
from ..matching_engine.matcher import rank_jobs

def _get_or_create_current_user(profile):
    """Uses the PROFILE's identity, so impressions land under the same user
    as tracker outcomes (audit fix: a hardcoded demo email split the event
    history across two users, which would poison learning-to-rank labels)."""
    from ..database.tracker import get_or_create_user
    return get_or_create_user(profile.get("email") or "local@user",
                              profile.get("name", ""))


def main(log_impressions_to_db=False):
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        profile = json.load(f)
    with open(DATA_DIR / "jobs.json", "r", encoding="utf-8") as f:
        jobs = json.load(f)

    print(f"Candidate skills: {[s['skill'] for s in profile.get('skills', [])]}")
    print(f"Total jobs loaded: {len(jobs)}")
    print("Ranking... (this downloads/loads the embedding model, may take a moment)")
    print()

    ranked = rank_jobs(profile, jobs, candidate_is_fresher=True)

    # Persist the ranked list so downstream tools (resume_tailor, cover
    # letters, Streamlit UI) don't have to re-run the whole ranking pipeline.
    with open(RANKED_FILE, "w", encoding="utf-8") as f:
        json.dump(ranked, f, ensure_ascii=False, indent=2)
    print(f"Saved ranked list to {RANKED_FILE}")

    print(f"Jobs remaining after dedup + hard gates: {len(ranked)}")
    print(f"(removed {len(jobs) - len(ranked)} via gates/dedup-at-rank-time)")
    print()
    print("=" * 90)
    print("TOP 10 MATCHES")
    print("=" * 90)
    for r in ranked[:10]:
        b = r["score_breakdown"]
        print(f"{r['match_score']:5.1f} | {r['title'][:45]:45} | {r['company']:15}")
        print(f"       skill={b['skill_overlap']:.2f}  semantic={b['semantic']:.2f}  "
              f"role={b['role']:.2f}  seniority={b['seniority']:.2f}")
        print(f"       strong_matches: {r['strong_matches'][:5]}")
        if r.get('likely_matches'):
            print(f"       likely_matches (category-level, partial credit): {r['likely_matches'][:5]}")
        print()

    # The key diagnostic: how many jobs have NONZERO skill_overlap now?
    nonzero_skill = sum(1 for r in ranked if r["score_breakdown"]["skill_overlap"] > 0)
    print("=" * 90)
    print(f"DIAGNOSTIC: {nonzero_skill} / {len(ranked)} jobs have skill_overlap > 0")
    print(f"(Previously, on RemoteOK-only data, this was 0 / 66)")

    if log_impressions_to_db:
        print()
        print("=" * 90)
        try:
            from ..database.events import log_impressions
            user_id = _get_or_create_current_user(profile)
            n = log_impressions(user_id, ranked)
            print(f"Logged {n} impression events to Postgres for user_id={user_id}")
        except Exception as exc:
            print(f"Could not log impressions to Postgres: {exc}")
            print("(Ranking itself still succeeded above -- this only affects logging.")
            print(" Check that `docker compose up -d` is running in backend/db/.)")


if __name__ == "__main__":
    main(log_impressions_to_db="--log" in sys.argv)