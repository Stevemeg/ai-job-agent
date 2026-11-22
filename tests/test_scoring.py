"""services.scoring without a database: the hashes that decide what gets
re-scored, the matcher fields they must cover, and assembly order. The
matcher is real; only the embedding similarity is stubbed (stub_semantic)."""
import json
from pathlib import Path

import pytest

from backend.services import scoring
from backend.services.scoring import Scored

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "sample_jobs.json"


def _job(**overrides):
    job = {"title": "AI Engineer", "company": "acme", "location": "Remote",
           "tags": ["Engineering"], "experience": "1 years", "seniority": "Junior",
           "clean_description": "Build RAG pipelines with FAISS and LLMs.",
           "apply_link": "https://x/1", "source": "ats",
           "postings": [{"source": "greenhouse", "posting_id": "1"}]}
    job.update(overrides)
    return job


def test_profile_hash_is_canonical_json(profile):
    reordered = dict(reversed(list(profile.items())))
    assert scoring.profile_hash(reordered) == scoring.profile_hash(profile)
    assert len(scoring.profile_hash(profile)) == 64
    changed = {**profile, "skills": profile["skills"][:-1]}
    assert scoring.profile_hash(changed) != scoring.profile_hash(profile)


@pytest.mark.parametrize("field,value,rescored", [
    ("clean_description", "Something else entirely.", True),
    ("tags", ["Research"], True),          # not in canonical content_hash
    ("seniority", "Senior", True),         # not in canonical content_hash
    ("title", "ai engineer", True),        # case: content_hash normalizes it
    ("apply_link", "https://x/2", False),  # display only, read fresh
    ("location", "Bengaluru", False),      # not a matcher input
    ("postings", [], False),
    ("experience", "5 years", False),
])
def test_score_input_hash_covers_exactly_the_matcher_inputs(field, value, rescored):
    before, after = _job(), _job(**{field: value})
    assert (scoring.score_input_hash(after) != scoring.score_input_hash(before)) \
        is rescored


class _Recording(dict):
    """A job dict that records every field read from it."""
    def __init__(self, data, seen):
        super().__init__(data)
        self._seen = seen

    def get(self, key, default=None):
        self._seen.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self._seen.add(key)
        return super().__getitem__(key)


def test_the_matcher_reads_no_field_outside_score_input_fields(profile,
                                                               stub_semantic):
    """If this fails the matcher started reading a new job field: add it to
    SCORE_INPUT_FIELDS (or cached scores go stale when only it changes) and
    bump FORMULA_VERSION."""
    score = scoring.matcher_scorer(profile)
    seen = set()
    for job in json.loads(SAMPLE.read_text(encoding="utf-8")):
        score(_Recording(job, seen))
    assert seen <= set(scoring.SCORE_INPUT_FIELDS)
    assert {"title", "clean_description"} <= seen


def test_score_job_breakdown_order_is_breakdown_keys(profile, stub_semantic):
    """Assembly restores this order on breakdowns read back from JSONB."""
    result = scoring.matcher_scorer(profile)(_job())
    assert result.passed
    assert tuple(result.score_breakdown) == scoring.BREAKDOWN_KEYS


def test_matcher_scorer_is_rank_jobs_per_job(profile, stub_semantic):
    from backend.matching_engine.matcher import rank_jobs
    jobs = json.loads(SAMPLE.read_text(encoding="utf-8"))
    score = scoring.matcher_scorer(profile)
    rows = list(enumerate(jobs))
    scores = {i: ("h", score(job)) for i, job in rows}
    assert any(not s.passed for _, s in scores.values())     # gates exercised
    assembled = scoring.assemble(scoring.first_seen(rows), scores)
    assert json.dumps(assembled) == json.dumps(
        rank_jobs(profile, jobs, candidate_is_fresher=True))


def test_first_seen_uses_the_matchers_case_insensitive_key():
    rows = [(1, _job(company="Acme")), (2, _job(company="acme ")),
            (3, _job(title="AI  ENGINEER")), (4, _job(title="ML Engineer"))]
    assert [i for i, _ in scoring.first_seen(rows)] == [1, 4]


def test_assemble_drops_gated_jobs_and_keeps_corpus_order_on_ties():
    def s(score):
        return Scored(True, score, {"semantic": 0.1, "skill_overlap": 0.2},
                      {"strong_matches": [], "likely_matches": [],
                       "missing_skills": []})
    reps = [(i, _job(title=f"Job {i}")) for i in range(4)]
    scores = {0: ("h", s(50.0)), 1: ("h", Scored(False)), 2: ("h", s(70.0)),
              3: ("h", s(50.0))}
    ranked = scoring.assemble(reps, scores)
    assert [r["title"] for r in ranked] == ["Job 2", "Job 0", "Job 3"]
    assert list(ranked[0]) == ["title", "company", "location", "match_score",
                               "score_breakdown", "strong_matches",
                               "likely_matches", "missing_skills", "apply_link"]


def test_stored_breakdowns_come_back_in_score_job_order():
    jsonb_order = {"role": 1.0, "semantic": 0.5, "seniority": 1.0,
                   "skill_overlap": 0.25}
    assert tuple(scoring._ordered_breakdown(jsonb_order)) == scoring.BREAKDOWN_KEYS
    assert scoring._ordered_breakdown(None) is None
