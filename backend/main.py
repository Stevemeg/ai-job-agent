"""
main.py -- end-to-end CLI pipeline: parse resume -> load corpus -> filter ->
rank -> save.

Run from project root:
    python -m backend.main          (or: python backend/main.py)

Reads uploads/resume.pdf. The corpus comes from the single corpus loader
(backend/database/corpus.py: Postgres when configured and reachable, else
data/jobs.json, else data/sample_jobs.json). If no corpus exists at all it
falls back to fetching RemoteOK and persists that the same way the ATS
collector does.

(Previously this used bare top-level imports and '../data' paths, which only
resolve when run from inside backend/ -- and even then failed, because the
matching engine imports `..config` relatively.)
"""
import json
import sys
from pathlib import Path

# Runnable both as `python -m backend.main` and as a plain script.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import DATA_DIR, UPLOADS_DIR, PROFILE_FILE, RANKED_FILE  # noqa: E402

from backend.resume_parser.resume_parser import (  # noqa: E402
    extract_text_from_pdf,
    extract_links_from_pdf,
    build_candidate_profile
)

from backend.resume_parser.text_cleaner import (  # noqa: E402
    clean_resume_text
)

from backend.job_scraper.job_collector import (  # noqa: E402
    fetch_remoteok_jobs
)

from backend.job_scraper.ats_collector import (  # noqa: E402
    save_collected_jobs
)

from backend.database.corpus import (  # noqa: E402
    load_corpus
)

from backend.matching_engine.matcher import (  # noqa: E402
    rank_jobs
)

from backend.matching_engine.domain_filter import (  # noqa: E402
    filter_jobs_by_domain
)


# -----------------------------------
# MAIN PIPELINE
# -----------------------------------
def run_pipeline():

    print("\n===== AI JOB AGENT STARTED =====\n")

    # -----------------------------------
    # CREATE DATA FOLDER
    # -----------------------------------
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    # -----------------------------------
    # RESUME PARSING
    # -----------------------------------
    pdf_path = str(UPLOADS_DIR / "resume.pdf")

    print("Parsing Resume...\n")

    raw_text = extract_text_from_pdf(
        pdf_path
    )

    cleaned_text = clean_resume_text(
        raw_text
    )

    pdf_links = extract_links_from_pdf(
        pdf_path
    )

    candidate_profile = build_candidate_profile(
        cleaned_text,
        pdf_links
    )

    # -----------------------------------
    # SAVE CANDIDATE PROFILE
    # -----------------------------------
    with open(
        PROFILE_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            candidate_profile,
            file,
            indent=4,
            ensure_ascii=False
        )

    print("Resume Parsed Successfully!\n")
    print("Candidate Profile Saved.\n")

    # -----------------------------------
    # JOB COLLECTION
    # -----------------------------------
    print("Loading Jobs...\n")

    jobs = load_corpus()

    if not jobs:

        print("No local jobs found.")
        print("Fetching from RemoteOK...\n")

        jobs = fetch_remoteok_jobs()

        save_collected_jobs(jobs)

        print(
            f"{len(jobs)} jobs saved.\n"
        )

    else:

        print(
            f"Loaded {len(jobs)} jobs "
            f"from the corpus.\n"
        )

    # -----------------------------------
    # DOMAIN FILTERING
    # -----------------------------------
    print("Filtering Jobs...\n")

    filtered_jobs = filter_jobs_by_domain(
        candidate_profile,
        jobs
    )

    print(
        f"Filtered {len(jobs)} jobs "
        f"to {len(filtered_jobs)} relevant jobs.\n"
    )

    # -----------------------------------
    # SAFETY FALLBACK
    # -----------------------------------
    if not filtered_jobs:

        print(
            "No domain-filtered jobs found.\n"
            "Using all jobs instead.\n"
        )

        filtered_jobs = jobs

    # -----------------------------------
    # JOB MATCHING
    # -----------------------------------
    print("Matching Jobs...\n")

    ranked_jobs = rank_jobs(
        candidate_profile,
        filtered_jobs
    )

    # -----------------------------------
    # SAVE RANKED JOBS
    # -----------------------------------
    with open(
        RANKED_FILE,
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            ranked_jobs,
            file,
            indent=4,
            ensure_ascii=False
        )

    print("Ranked Jobs Saved.\n")

    # -----------------------------------
    # TOP MATCHES
    # -----------------------------------
    top_jobs = ranked_jobs[:10]

    print(
        "\n===== TOP JOB MATCHES =====\n"
    )

    print(
        json.dumps(
            top_jobs,
            indent=4,
            ensure_ascii=False
        )
    )


# -----------------------------------
# RUN
# -----------------------------------
if __name__ == "__main__":

    run_pipeline()