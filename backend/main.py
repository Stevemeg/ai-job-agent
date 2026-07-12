import json
import os

from resume_parser.resume_parser import (
    extract_text_from_pdf,
    extract_links_from_pdf,
    build_candidate_profile
)

from resume_parser.text_cleaner import (
    clean_resume_text
)

from job_scraper.job_collector import (
    fetch_remoteok_jobs
)

from job_scraper.job_database import (
    save_jobs,
    load_jobs
)

from matching_engine.matcher import (
    rank_jobs
)

from matching_engine.domain_filter import (
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
    os.makedirs(
        "../data",
        exist_ok=True
    )

    # -----------------------------------
    # RESUME PARSING
    # -----------------------------------
    pdf_path = "../uploads/resume.pdf"

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
        "../data/candidate_profile.json",
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

    jobs = load_jobs()

    if not jobs:

        print("No local jobs found.")
        print("Fetching from RemoteOK...\n")

        jobs = fetch_remoteok_jobs()

        save_jobs(jobs)

        print(
            f"{len(jobs)} jobs saved locally.\n"
        )

    else:

        print(
            f"Loaded {len(jobs)} jobs "
            f"from database.\n"
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
        "../data/ranked_jobs.json",
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