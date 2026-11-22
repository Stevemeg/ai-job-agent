"""Streamlit jobs view: demo mode (no DB) still ranks inline; queue mode (DB
configured) enqueues through services.ranking and never ranks in-process.
Runs the real view under streamlit.testing (skipped where Streamlit isn't
installed, e.g. the slim CI install); the ranking engine and the queue are
stubbed."""
import json

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest           # noqa: E402

import backend.ui.views.jobs as jobs_view          # noqa: E402
from backend.database import corpus                # noqa: E402


def _view():
    import json as _json
    from backend.config import PROFILE_FILE
    from backend.ui.views import jobs
    with open(PROFILE_FILE, encoding="utf-8") as f:
        jobs.render(_json.load(f))


@pytest.fixture
def page(tmp_path, monkeypatch, profile):
    import backend.config as config
    profile_file = tmp_path / "candidate_profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    jobs_file = tmp_path / "jobs.json"
    jobs_file.write_text(json.dumps([{"title": "AI Engineer", "company": "acme",
                                      "clean_description": "LLMs"}]),
                         encoding="utf-8")
    ranked_file = tmp_path / "ranked_jobs.json"
    monkeypatch.setattr(config, "PROFILE_FILE", profile_file)
    monkeypatch.setattr(jobs_view, "RANKED_FILE", ranked_file)
    monkeypatch.setattr(corpus, "JOBS_FILE", jobs_file)
    return ranked_file


def _ranked(n=12):            # the view needs >= 10 (its "Show top N" minimum)
    return [{"title": f"AI Engineer {i}", "company": "acme", "location": None,
             "match_score": 80.0, "score_breakdown": {"skill_overlap": 0.5},
             "strong_matches": [], "missing_skills": [], "apply_link": None}
            for i in range(n)]


def test_demo_mode_ranks_inline_exactly_as_before(page, monkeypatch):
    pytest.importorskip("sklearn")         # matcher's import chain
    import backend.matching_engine.matcher as matcher
    calls = []
    monkeypatch.setattr(matcher, "rank_jobs",
                        lambda p, j, candidate_is_fresher: calls.append(j) or _ranked())

    def no_queue(*a, **k):
        raise AssertionError("demo mode must not touch the queue")
    import backend.services.ranking as ranking
    monkeypatch.setattr(ranking, "request_ranking", no_queue)

    at = AppTest.from_function(_view).run()
    assert not at.exception
    button = next(b for b in at.button if b.label == "Rank jobs now")
    at = button.click().run()
    assert not at.exception
    assert len(calls) == 1 and calls[0][0]["title"] == "AI Engineer"
    assert json.loads(page.read_text(encoding="utf-8")) == _ranked()


def test_queue_mode_enqueues_and_shows_status_without_ranking_inline(
        page, monkeypatch):
    monkeypatch.setenv("DB_HOST", "db.invalid")        # configured
    import backend.services.ranking as ranking
    state = {"task": None}

    def request_ranking():
        state["task"] = {"id": 7, "status": "queued", "attempts": 0,
                         "max_attempts": 3, "last_error": None, "result": None}
        return 7

    monkeypatch.setattr(ranking, "request_ranking", request_ranking)
    monkeypatch.setattr(ranking, "latest_rank_task", lambda: state["task"])
    monkeypatch.setattr(jobs_view, "_rerank_inline",
                        lambda p: pytest.fail("queue mode ranked in-process"))
    # Keep the corpus read on the JSON file (no real DB behind DB_HOST).
    monkeypatch.setattr(corpus, "db_available", lambda: False)

    at = AppTest.from_function(_view).run()
    assert not at.exception
    button = next(b for b in at.button if b.label == "Rank jobs now")
    at = button.click().run()
    assert not at.exception
    assert any("Queued ranking task #7" in s.value for s in at.success)
    assert any("waiting for a worker" in i.value for i in at.info)
    assert not page.exists()

    # A second click while it is active reports the conflict.
    from backend.taskqueue import ConcurrencyConflict

    def busy():
        raise ConcurrencyConflict("rank:default")
    monkeypatch.setattr(ranking, "request_ranking", busy)
    at = next(b for b in at.button if b.label == "Rank jobs now").click().run()
    assert any("already in progress" in i.value for i in at.info)
