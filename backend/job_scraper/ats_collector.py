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
zero changes downstream. Each job also carries "postings": the ATS's own
posting id (plus location/link), which canonical_jobs uses to keep the same
title posted in several locations as ONE job with several postings.

Where results go (save_collected_jobs):
  - database configured + reachable -> upsert into canonical_jobs
  - otherwise -> deduped merge into data/jobs.json

Two ways to collect:
  - collect_all_ats_jobs() (this CLI): a board that fails is printed and
    skipped, as before.
  - collect_sources() (the scheduled scan, services/scan.py): the same
    fetchers with the same retry/backoff, but each source is isolated and a
    failure is REPORTED -- {source, slug, error} -- instead of looking like
    an empty board. No new sources: these three public ATS APIs only
    (ADR-001 guardrail 3).

Run (from project root, one level above backend/):
    python -m backend.job_scraper.ats_collector
"""
import time
from dataclasses import dataclass, field

import requests

from .job_parser import clean_job_description, extract_experience, detect_seniority
from ..config import JOBS_FILE

# --------------------------------------------------------------------------
# SEED COMPANIES — curated, AI/ML/Data/Backend-heavy. Add more as you find
# them: visit https://job-boards.greenhouse.io/<token>, https://jobs.lever.co/
# <company>, or https://jobs.ashbyhq.com/<name> to discover a company's token.
# --------------------------------------------------------------------------
GREENHOUSE_COMPANIES = [
    # DoorDash's board token is "doordashusa" (board name "DoorDash USA");
    # "doordash" returns 404 (checked 2026-11-23 against boards-api).
    "stripe", "airbnb", "robinhood", "doordashusa", "coinbase", "asana",
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
    # Perplexity's job board name is "perplexity"; "perplexity-ai" returns
    # 404 (checked 2026-11-23 against the posting API).
    "ramp", "openai", "linear", "watershed", "mercury", "vanta",
    "modal", "replit", "perplexity", "deel",
]

REQUEST_TIMEOUT = 10
RETRY_COUNT = 2
RETRY_BACKOFF_SECONDS = 1.5
USER_AGENT = "Mozilla/5.0 (AI-Job-Agent/1.0; personal portfolio project)"


class SourceFetchError(RuntimeError):
    """One ATS board could not be fetched or parsed (after the retries)."""


def _fetch_json(url, params=None):
    """GET with small retry/backoff; the parsed JSON, or SourceFetchError
    describing the last failure. A 404 is not retried (bad/renamed company
    token); other statuses (429, 5xx), network errors and unparseable
    bodies are. A rate limit or block is backed off from and reported,
    never worked around (ADR-001 guardrail 4)."""
    headers = {"User-Agent": USER_AGENT}
    last = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            resp = requests.get(url, headers=headers, params=params,
                                 timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                raise SourceFetchError(
                    "HTTP 404 (board not found -- renamed or removed token?)")
            last = f"HTTP {resp.status_code}"
        except requests.RequestException as exc:   # incl. invalid JSON
            last = f"{exc.__class__.__name__}: {exc}"
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise SourceFetchError(f"{last} (gave up after {RETRY_COUNT + 1} attempts)")


def _get_with_retries(url, params=None):
    """GET with small retry/backoff. Returns parsed JSON or None on failure.
    Failures here are routine (company renamed token, board taken down,
    transient network blip) — log and move on, never crash the whole run."""
    try:
        return _fetch_json(url, params)
    except SourceFetchError:
        return None


def _board(label, slug, url, params=None, jobs_key="jobs", strict=False):
    """A board's JSON, or None when it has nothing usable. strict=True
    raises SourceFetchError instead, so the caller can report why."""
    if not strict:
        data = _get_with_retries(url, params=params)
        if not data or (jobs_key and jobs_key not in data):
            print(f"  [{label}] {slug}: no data / board not found")
            return None
        return data
    data = _fetch_json(url, params)
    if jobs_key and not (isinstance(data, dict)
                         and isinstance(data.get(jobs_key), list)):
        raise SourceFetchError(f"unexpected response: no {jobs_key!r} list")
    if not jobs_key and not isinstance(data, list):
        raise SourceFetchError("unexpected response: not a list of postings")
    return data


def _normalize(title, company, location, tags, description_html, apply_link,
               ats=None, posting_id=None):
    """Build the exact dict shape job_parser.parse_job already produces,
    so output is a drop-in match for the existing pipeline -- plus the ATS
    posting identity, which the matcher ignores and the corpus layer keys
    postings on."""
    cleaned = clean_job_description(description_html or "")
    postings = []
    if posting_id not in (None, ""):
        postings.append({"source": ats, "posting_id": str(posting_id),
                         "location": location, "apply_link": apply_link})
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
        "postings": postings,     # [{source: greenhouse|lever|ashby, posting_id, ...}]
    }


# --------------------------------------------------------------------------
# GREENHOUSE
# --------------------------------------------------------------------------
def fetch_greenhouse_jobs(company_token, strict=False):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company_token}/jobs"
    data = _board("greenhouse", company_token, url, {"content": "true"},
                  strict=strict)
    if data is None:
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
            ats="greenhouse",
            posting_id=j.get("id"),     # per-posting id (not internal_job_id)
        ))
    print(f"  [greenhouse] {company_token}: {len(jobs)} jobs")
    return jobs


# --------------------------------------------------------------------------
# LEVER
# --------------------------------------------------------------------------
def fetch_lever_jobs(company_slug, strict=False):
    url = f"https://api.lever.co/v0/postings/{company_slug}"
    data = _board("lever", company_slug, url, {"mode": "json"}, jobs_key=None,
                  strict=strict)
    if data is None:
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
            ats="lever",
            posting_id=j.get("id"),
        ))
    print(f"  [lever] {company_slug}: {len(jobs)} jobs")
    return jobs


# --------------------------------------------------------------------------
# ASHBY
# --------------------------------------------------------------------------
def fetch_ashby_jobs(job_board_name, strict=False):
    url = f"https://api.ashbyhq.com/posting-api/job-board/{job_board_name}"
    data = _board("ashby", job_board_name, url, strict=strict)
    if data is None:
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
            ats="ashby",
            posting_id=j.get("id"),
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


FETCHERS = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
}


def default_sources():
    """Every configured (ats, slug) pair, read at call time."""
    return ([("greenhouse", s) for s in GREENHOUSE_COMPANIES]
            + [("lever", s) for s in LEVER_COMPANIES]
            + [("ashby", s) for s in ASHBY_COMPANIES])


@dataclass
class Collection:
    """What collect_sources() gathered, and from where."""
    jobs: list = field(default_factory=list)
    sources_ok: list = field(default_factory=list)      # [(ats, slug)]
    sources_failed: list = field(default_factory=list)  # [{source, slug, error}]


def collect_sources(sources=None):
    """Fetch every source (default: default_sources()), isolating each one:
    a source that raises -- network failure after the retries, a 404, an
    unexpected payload, or a bug parsing it -- is recorded in
    sources_failed and the rest still run. An empty board is a success."""
    out = Collection()
    for ats, slug in (default_sources() if sources is None else sources):
        try:
            jobs = FETCHERS[ats](slug, strict=True)
        except Exception as exc:                            # noqa: BLE001
            error = (str(exc) if isinstance(exc, SourceFetchError)
                     else f"{exc.__class__.__name__}: {exc}")
            print(f"  [{ats}] {slug}: FAILED -- {error}")
            out.sources_failed.append({"source": ats, "slug": slug,
                                       "error": error})
            continue
        out.sources_ok.append((ats, slug))
        out.jobs.extend(jobs)
    return out


def merge_into_jobs_file(new_jobs, jobs_path=None):
    """JSON-only path (no database): merge ATS jobs into data/jobs.json,
    DEDUPED on (company, normalized title) -- the canonical_jobs key --
    instead of appending (appending duplicated every job on each re-run).
    Same rules as the DB upsert: a job keeps its first position, the latest
    sighting's fields win, postings are merged. Existing entries from other
    sources (RemoteOK) stay. Returns {"total", "added", "updated"}."""
    import json
    from pathlib import Path
    from ..database.corpus import dedupe_jobs, job_key, write_json_atomic

    jobs_path = Path(jobs_path) if jobs_path is not None else JOBS_FILE
    existing = []
    if jobs_path.exists():
        with open(jobs_path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    known = {job_key(j) for j in existing}
    incoming = {job_key(j) for j in new_jobs}
    combined = dedupe_jobs(existing + list(new_jobs))
    write_json_atomic(jobs_path, combined)

    stats = {"total": len(combined), "added": len(incoming - known),
             "updated": len(incoming & known)}
    print(f"Wrote {stats['total']} jobs to {jobs_path} "
          f"({stats['added']} new, {stats['updated']} re-sighted, "
          f"{len(existing)} before)")
    return stats


def save_collected_jobs(jobs):
    """Persist a collection run: upsert into canonical_jobs when a database
    is configured and reachable, else dedupe-merge into data/jobs.json. A DB
    write failure raises rather than silently writing JSON instead -- that
    would quietly split the corpus across two stores."""
    from ..database.corpus import db_available, upsert_jobs

    if db_available():
        stats = upsert_jobs(jobs)
        print(f"Upserted into canonical_jobs: {stats['inserted']} new, "
              f"{stats['updated']} re-sighted ({stats['changed']} changed)")
        return stats
    print("No database configured/reachable -- writing the JSON corpus.")
    return merge_into_jobs_file(jobs)


if __name__ == "__main__":
    jobs = collect_all_ats_jobs()
    save_collected_jobs(jobs)