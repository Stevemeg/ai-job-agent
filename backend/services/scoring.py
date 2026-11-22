"""
scoring.py -- incremental scoring for ranking (Phase 3, ADR-001 §1/§4).

rank_jobs(profile, corpus) re-scores every job on every run: minutes of
embedding work for a corpus that barely changes between scans. This module
caches each job's matcher output in job_scores, keyed on
(job_id, profile_hash, formula_version), and scores only the delta:

    a job NEEDS SCORING  <=>  there is no job_scores row for
        (job_id, profile_hash, FORMULA_VERSION) whose content_hash is the
        job's current score_input_hash()

The delta comes from job_scores alone, never from "rows this scan changed":
a retry after a crash, a scan that waited behind another rank, and jobs
someone else upserted meanwhile are all picked up by the next run. Gate
failures are stored too (passed = false), so they are not re-evaluated.

Assembly reproduces rank_jobs exactly (a parity test pins it): walk the
corpus in corpus order, apply the matcher's own _job_key first-seen dedup,
keep the rows that passed the gates, stable-sort by match_score descending.
Display fields (title, company, location, apply_link) come from the current
corpus row; the score, breakdown and explanation from job_scores.

The scoring itself is the matcher's (passes_hard_filters, score_job,
explain_match), called exactly as rank_jobs calls them. Callers hold the
rank lock (services/ranking.py) for the whole score-and-assemble, so the
rows of one (profile, formula) have one writer at a time.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .. import version
from ..config import WEIGHTS

# rank_jobs' candidate_is_fresher, as the ranking service has always run it.
CANDIDATE_IS_FRESHER = True

# Every job field the matcher reads (gates, sub-scores, explainer). A cached
# score is valid only while all of them are unchanged, so these -- not
# canonical_jobs.content_hash, which leaves out tags and seniority and
# normalizes title case and whitespace -- are what score_input_hash()
# covers. A test fails if the matcher starts reading another field; reading
# one is a scoring change anyway (bump FORMULA_VERSION).
SCORE_INPUT_FIELDS = ("title", "clean_description", "tags", "seniority")

# score_job's breakdown key order (a test pins it). JSONB does not keep
# object key order, so stored breakdowns are put back in this order: the
# ranked file stays byte-for-byte what rank_jobs writes.
BREAKDOWN_KEYS = tuple(WEIGHTS)

SAVE_BATCH = 200       # job_scores rows per transaction while scoring


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def profile_hash(profile: dict) -> str:
    """sha256 of the profile's canonical JSON (sorted keys, no whitespace)."""
    return hashlib.sha256(_canonical_json(profile)).hexdigest()


def score_input_hash(job: dict) -> str:
    """sha256 over exactly the job fields the matcher reads."""
    return hashlib.sha256(_canonical_json(
        {f: job.get(f) for f in SCORE_INPUT_FIELDS})).hexdigest()


@dataclass(frozen=True)
class Scored:
    """The matcher's verdict on one job -- what rank_jobs computes per job."""
    passed: bool
    match_score: float | None = None
    score_breakdown: dict | None = None
    explanation: dict | None = None       # {strong_matches, likely_matches, missing_skills}


Scorer = Callable[[dict], Scored]


def matcher_scorer(profile: dict) -> Scorer:
    """Score one job exactly as rank_jobs does: the hard gates, then
    score_job, then explain_match. The embedding model loads on the first
    semantic score, so building a scorer is cheap."""
    from ..matching_engine import matcher
    skills = [s["skill"] for s in profile.get("skills", [])]
    candidate_text = matcher.build_candidate_text(profile)

    def score(job: dict) -> Scored:
        if not matcher.passes_hard_filters(job, CANDIDATE_IS_FRESHER):
            return Scored(passed=False)
        value, breakdown = matcher.score_job(skills, candidate_text, job,
                                             CANDIDATE_IS_FRESHER)
        return Scored(True, value, breakdown,
                      matcher.explain_match(profile, job))
    return score


def first_seen(rows: Iterable[tuple[int, dict]]) -> list[tuple[int, dict]]:
    """rank_jobs' dedup: keep the first (job_id, job) per matcher._job_key
    (lowercased company + normalized title), in corpus order. Only these
    are ever scored or ranked."""
    from ..matching_engine.matcher import _job_key
    seen, out = set(), []
    for job_id, job in rows:
        key = _job_key(job)
        if key not in seen:
            seen.add(key)
            out.append((job_id, job))
    return out


# --------------------------------------------------------------------------
# job_scores
# --------------------------------------------------------------------------
def _ordered_breakdown(stored: dict | None) -> dict | None:
    if stored is None:
        return None
    out = {k: stored[k] for k in BREAKDOWN_KEYS if k in stored}
    out.update((k, v) for k, v in stored.items() if k not in out)
    return out


def load_scores(conn: Any, p_hash: str,
                f_version: str) -> dict[int, tuple[str, Scored]]:
    """{job_id: (content_hash, Scored)} for one (profile, formula)."""
    from sqlalchemy import select
    from ..database.db import JobScore
    t = JobScore.__table__
    rows = conn.execute(
        select(t.c.job_id, t.c.content_hash, t.c.passed, t.c.match_score,
               t.c.score_breakdown, t.c.explanation)
        .where(t.c.profile_hash == p_hash, t.c.formula_version == f_version))
    return {r.job_id: (r.content_hash, Scored(
        passed=r.passed,
        match_score=None if r.match_score is None else float(r.match_score),
        score_breakdown=_ordered_breakdown(r.score_breakdown),
        explanation=r.explanation)) for r in rows}


def save_scores(conn: Any, p_hash: str, f_version: str,
                items: list[tuple[int, str, Scored]]) -> None:
    """Upsert (job_id, content_hash, Scored) rows for one (profile, formula)."""
    if not items:
        return
    from sqlalchemy import func
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from ..database.db import JobScore
    t = JobScore.__table__
    stmt = pg_insert(t)
    stmt = stmt.on_conflict_do_update(
        index_elements=[t.c.job_id, t.c.profile_hash, t.c.formula_version],
        set_={**{c: stmt.excluded[c] for c in (
            "content_hash", "passed", "match_score", "score_breakdown",
            "explanation")}, "scored_at": func.now()})
    conn.execute(stmt, [
        {"job_id": job_id, "profile_hash": p_hash, "formula_version": f_version,
         "content_hash": c_hash, "passed": s.passed, "match_score": s.match_score,
         "score_breakdown": s.score_breakdown, "explanation": s.explanation}
        for job_id, c_hash, s in items])


# --------------------------------------------------------------------------
# assembly + the incremental run
# --------------------------------------------------------------------------
def ranked_entry(job: dict, scored: Scored) -> dict:
    """One RANKED_FILE entry -- rank_jobs' dict, key for key."""
    explanation = scored.explanation
    return {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "match_score": scored.match_score,
        "score_breakdown": scored.score_breakdown,
        "strong_matches": explanation["strong_matches"],
        "likely_matches": explanation["likely_matches"],
        "missing_skills": explanation["missing_skills"],
        "apply_link": job.get("apply_link"),
    }


def assemble(reps: list[tuple[int, dict]],
             scores: dict[int, tuple[str, Scored]]) -> list[dict]:
    """The ranked list from first-seen rows (corpus order) and their scores:
    passed rows only, stable-sorted by match_score descending -- the same
    sorted() call rank_jobs makes, so ties keep corpus order."""
    ranked = [ranked_entry(job, scores[job_id][1]) for job_id, job in reps
              if scores[job_id][1].passed]
    return sorted(ranked, key=lambda x: x["match_score"], reverse=True)


@dataclass(frozen=True)
class RankOutcome:
    ranked: list
    scored: int                  # jobs scored (or gate-checked) this run
    skipped_unchanged: int       # first-seen jobs whose stored score was current
    profile_hash: str
    formula_version: str


def rank_incrementally(rows: list[tuple[int, dict]], profile: dict, *,
                       make_scorer: Callable[[dict], Scorer] = matcher_scorer,
                       ctx: Any = None, batch: int = SAVE_BATCH) -> RankOutcome:
    """Score the delta of `rows` ((canonical_jobs.id, job) in corpus order)
    for `profile`, store it, and assemble the full ranked list.

    Scores are saved every `batch` jobs, each batch in its own transaction,
    so a run that dies part-way keeps what it finished and its retry only
    scores the rest. With a worker `ctx`, the lease is checked before every
    job: a worker that lost its task stops instead of scoring on."""
    from ..database.db import get_engine, init_db
    init_db()
    p_hash = profile_hash(profile)
    f_version = version.FORMULA_VERSION
    reps = first_seen(rows)
    engine = get_engine()
    with engine.connect() as conn:
        scores = load_scores(conn, p_hash, f_version)

    current = {job_id: score_input_hash(job) for job_id, job in reps}
    delta = [(job_id, job) for job_id, job in reps
             if job_id not in scores or scores[job_id][0] != current[job_id]]
    if delta:
        scorer = make_scorer(profile)
        pending: list[tuple[int, str, Scored]] = []
        for job_id, job in delta:
            if ctx is not None:
                ctx.ensure_lease()
            result = scorer(job)
            scores[job_id] = (current[job_id], result)
            pending.append((job_id, current[job_id], result))
            if len(pending) >= batch:
                with engine.begin() as conn:
                    save_scores(conn, p_hash, f_version, pending)
                pending = []
        with engine.begin() as conn:
            save_scores(conn, p_hash, f_version, pending)

    return RankOutcome(ranked=assemble(reps, scores), scored=len(delta),
                       skipped_unchanged=len(reps) - len(delta),
                       profile_hash=p_hash, formula_version=f_version)
