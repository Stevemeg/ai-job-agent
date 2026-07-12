"""
ats_collector.py — Phase 1 of the redesigned roadmap.

Pulls real, legally-sourced job postings directly from company ATS boards
(Greenhouse, Lever, Ashby) instead of scraping LinkedIn/Naukri/Indeed, which
violates ToS. All three endpoints below are public, unauthenticated GET APIs
documented by their respective platforms:

  Greenhouse : GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
  Lever      : GET https://api.lever.co/v0/postings/{company}?mode=json
  Ashby      : GET https://api.ashbyhq.com/posting-api/job-board/{name}

Output is normalized into the SAME schema your existing job_parser.parse_job
already produces, by reusing its clean_job_description / extract_experience /
detect_seniority helpers — so this plugs directly into matcher.rank_jobs with
zero changes downstream.

Run (from project root, one level above backend/):
    python -m backend.job_scraper.ats_collector
"""
import time
import requests

from .job_parser import clean_job_description, extract_experience, detect_seniority
from ..config import DATA_DIR

# --------------------------------------------------------------------------
# SEED COMPANIES — curated, AI/ML/Data/Backend-heavy. Add more as you find
# them: visit https://job-boards.greenhouse.io/<token>, https://jobs.lever.co/
# <company>, or https://jobs.ashbyhq.com/<name> to discover a company's token.
# --------------------------------------------------------------------------
GREENHOUSE_COMPANIES = [
    "stripe", "airbnb", "robinhood", "doordash", "coinbase", "asana",
    "discord", "pinterest", "lyft", "instacart", "affirm", "gitlab",
    "databricks", "scaleai", "anthropic",
]

LEVER_COMPANIES = [
    # Disabled for now. 11 guessed slugs (including "attentive", verified
    # directly against the live API as returning {"ok":false,"error":
    # "Document not found"}) all failed. Unlike Greenhouse/Ashby, there's no
    # reliable public directory of Lever-hosted companies to guess from
    # outside the platform, so blind seeding doesn't work here.
    #
    # To re-enable: if you ever see a careers page at jobs.lever.co/<slug>
    # while job-hunting normally, that slug is confirmed real — add it here.
]

ASHBY_COMPANIES = [
    "ramp", "openai", "linear", "watershed", "mercury", "vanta",
    "modal", "replit", "perplexity-ai", "deel",
]

REQUEST_TIMEOUT = 10
RETRY_COUNT = 2
RETRY_BACKOFF_SECONDS = 1.5
USER_AGENT = "Mozilla/5.0 (AI-Job-Agent/1.0; personal portfolio project)"


def _get_with_retries(url, params=None):
    """GET with small retry/backoff. Returns parsed JSON or None on failure.
    Failures here are routine (company renamed token, board taken down,
    transient network blip) — log and move on, never crash the whole run."""
    headers = {"User-Agent": USER_AGENT}
    for attempt in range(RETRY_COUNT + 1):
        try:
            resp = requests.get(url, headers=headers, params=params,
                                 timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None          # bad/renamed company token — not worth retrying
            # other statuses (429, 5xx) are worth a retry
        except requests.RequestException:
            pass
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None


def _normalize(title, company, location, tags, description_html, apply_link):
    """Build the exact dict shape job_parser.parse_job already produces,
    so output is a drop-in match for the existing pipeline."""
    cleaned = clean_job_description(description_html or "")
    return {
        "title": title,
        "company": company,
        "location": location,
        "tags": tags or [],
        "experience": extract_experience(cleaned),
        "seniority": detect_seniority(title or ""),
        "clean_description": cleaned,
        "apply_link": apply_link,
        "source": "ats",          # lets you trace provenance later
    }


# --------------------------------------------------------------------------
# GREENHOUSE
# --------------------------------------------------------------------------
def fetch_greenhouse_jobs(company_token):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_token}/jobs"
    data = _get_with_retries(url, params={"content": "true"})
    if not data or "jobs" not in data:
        print(f"  [greenhouse] {company_token}: no data / board not found")
        return []

    jobs = []
    for j in data["jobs"]:
        location = (j.get("location") or {}).get("name")
        tags = [d.get("name") for d in j.get("departments", []) if d.get("name")]
        jobs.append(_normalize(
            title=j.get("title"),
            company=company_token,
            location=location,
            tags=tags,
            description_html=j.get("content"),
            apply_link=j.get("absolute_url"),
        ))
    print(f"  [greenhouse] {company_token}: {len(jobs)} jobs")
    return jobs


# --------------------------------------------------------------------------
# LEVER
# --------------------------------------------------------------------------
def fetch_lever_jobs(company_slug):
    url = f"https://api.lever.co/v0/postings/{company_slug}"
    data = _get_with_retries(url, params={"mode": "json"})
    if not data:
        print(f"  [lever] {company_slug}: no data / board not found")
        return []

    jobs = []
    for j in data:
        categories = j.get("categories", {}) or {}
        location = categories.get("location")
        tags = [t for t in [categories.get("team"), categories.get("department"),
                             categories.get("commitment")] if t]
        jobs.append(_normalize(
            title=j.get("text"),
            company=company_slug,
            location=location,
            tags=tags,
            description_html=j.get("descriptionPlain") or j.get("description"),
            apply_link=j.get("applyUrl") or j.get("hostedUrl"),
        ))
    print(f"  [lever] {company_slug}: {len(jobs)} jobs")
    return jobs


# --------------------------------------------------------------------------
# ASHBY
# --------------------------------------------------------------------------
def fetch_ashby_jobs(job_board_name):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{job_board_name}"
    data = _get_with_retries(url)
    if not data or "jobs" not in data:
        print(f"  [ashby] {job_board_name}: no data / board not found")
        return []

    jobs = []
    for j in data["jobs"]:
        tags = [t for t in [j.get("department"), j.get("team")] if t]
        jobs.append(_normalize(
            title=j.get("title"),
            company=job_board_name,
            location=j.get("location"),
            tags=tags,
            description_html=j.get("descriptionPlain") or j.get("descriptionHtml"),
            apply_link=j.get("applyUrl") or j.get("jobUrl"),
        ))
    print(f"  [ashby] {job_board_name}: {len(jobs)} jobs")
    return jobs


# --------------------------------------------------------------------------
# ORCHESTRATION
# --------------------------------------------------------------------------
def collect_all_ats_jobs():
    """Hits every seed company across all three ATS sources.
    NOTE: dedup across sources is handled downstream by matcher.rank_jobs
    (canonical key = company + normalized title), not here — this function's
    job is supply, not cleanup."""
    all_jobs = []

    print("Fetching Greenhouse boards...")
    for company in GREENHOUSE_COMPANIES:
        all_jobs.extend(fetch_greenhouse_jobs(company))

    print("Fetching Lever boards...")
    for company in LEVER_COMPANIES:
        all_jobs.extend(fetch_lever_jobs(company))

    print("Fetching Ashby boards...")
    for company in ASHBY_COMPANIES:
        all_jobs.extend(fetch_ashby_jobs(company))

    print(f"\nTotal raw jobs collected: {len(all_jobs)}")
    return all_jobs


def merge_into_jobs_file(new_jobs):
    """Appends ATS jobs onto the existing data/jobs.json (RemoteOK jobs stay).
    Uses config.JOBS_FILE so this respects the same path your matcher reads
    from — avoids the '../data' fragility the original job_database.py had."""
    import json

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    jobs_path = DATA_DIR / "jobs.json"

    existing = []
    if jobs_path.exists():
        with open(jobs_path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    combined = existing + new_jobs
    with open(jobs_path, "w", encoding="utf-8") as f:
        json.dump(combined, f, indent=4, ensure_ascii=False)

    print(f"Wrote {len(combined)} total jobs to {jobs_path} "
          f"({len(existing)} existing + {len(new_jobs)} new from ATS)")


if __name__ == "__main__":
    jobs = collect_all_ats_jobs()
    merge_into_jobs_file(jobs)