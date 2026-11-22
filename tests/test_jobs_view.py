"""Streamlit jobs view: demo mode (no DB) still ranks inline; queue mode (DB
configured) enqueues through services.ranking and never ranks in-process.
Runs the real view under streamlit.testing; the ranking engine and the
queue are stubbed.

Streamlit is skipped per test (in the fixture), never at module level: the
slim CI install has no Streamlit, and a module-level skip would be reported
even by the `-m db` run, whose "no skipped tests" guard would then fail."""
import json
import sys
import types

import pytest

from backend.database import corpus


@pytest.fixture
def jobs_view():
    pytest.importorskip("streamlit")
    import backend.ui.views.jobs as view
    return view


def _app():
    from streamlit.testing.v1 import AppTest
    return AppTest.from_function(_view)


def _view():
    import json as _json
    from backend.config import PROFILE_FILE
    from backend.ui.views import jobs
    with open(PROFILE_FILE, encoding="utf-8") as f:
        jobs.render(_json.load(f))


@pytest.fixture
def page(tmp_path, monkeypatch, profile, jobs_view):
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


def _ranked(n=12):
    return [{"title": f"AI Engineer {i}", "company": "acme", "location": None,
             "match_score": 80.0, "score_breakdown": {"skill_overlap": 0.5},
             "strong_matches": [], "missing_skills": [], "apply_link": None}
            for i in range(n)]


def _stub_matcher(monkeypatch, rank_jobs):
    """Stand-in for backend.matching_engine.matcher. Importing the real one
    pulls in sklearn and sentence-transformers (torch); a stub module in
    sys.modules means the view's deferred `from ...matcher import rank_jobs`
    never touches them."""
    fake = types.ModuleType("backend.matching_engine.matcher")
    fake.rank_jobs = rank_jobs
    monkeypatch.setitem(sys.modules, "backend.matching_engine.matcher", fake)


def test_demo_mode_ranks_inline_exactly_as_before(page, monkeypatch):
    calls = []
    _stub_matcher(monkeypatch,
                  lambda p, j, candidate_is_fresher: calls.append(j) or _ranked())

    def no_queue(*a, **k):
        raise AssertionError("demo mode must not touch the queue")
    import backend.services.ranking as ranking
    monkeypatch.setattr(ranking, "request_ranking", no_queue)

    at = _app().run()
    assert not at.exception
    button = next(b for b in at.button if b.label == "Rank jobs now")
    at = button.click().run()
    assert not at.exception
    assert len(calls) == 1 and calls[0][0]["title"] == "AI Engineer"
    assert json.loads(page.read_text(encoding="utf-8")) == _ranked()


def test_queue_mode_enqueues_and_shows_status_without_ranking_inline(
        page, monkeypatch, jobs_view):
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

    at = _app().run()
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


@pytest.mark.parametrize("n_ranked", [0, 3, 9, 10, 45])
def test_show_top_n_works_for_any_number_of_ranked_jobs(page, n_ranked):
    """Fewer than 10 ranked jobs used to crash the page: "Show top N" had a
    fixed minimum of 10, above its maximum of len(ranked)."""
    page.write_text(json.dumps(_ranked(n_ranked)), encoding="utf-8")
    at = _app().run()
    assert not at.exception
    top_n = next(n for n in at.number_input if n.label == "Show top N")
    assert top_n.value == min(30, n_ranked)
    assert any(f"of {n_ranked:,} ranked jobs" in m.value for m in at.markdown)
