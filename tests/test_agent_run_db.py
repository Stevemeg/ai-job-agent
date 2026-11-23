"""The agent run against a real Postgres: triage picks exactly the jobs the
spec allows, dispatch writes the application and its task atomically, and
re-running (or resuming) a run never duplicates an application."""
import json

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.agent import run_graph, store        # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.services import scoring              # noqa: E402
from backend.worker import Worker                 # noqa: E402

pytestmark = pytest.mark.db

PROFILE_HASH, CACHE_KEY = "p" * 64, "1:abcdef012345"


def _job(title, company="acme"):
    return {"title": title, "company": company, "location": "Remote",
            "tags": [], "experience": "1 years", "seniority": "Junior",
            "clean_description": f"{title}: RAG with FAISS.",
            "apply_link": f"https://x/{title}", "source": "ats"}


def _seed(pg, specs):
    """specs: title -> (score, passed, days_since_seen, profile_hash, cache_key).
    Returns {title: job_id}."""
    corpus.upsert_jobs([_job(t) for t in specs])
    with pg.begin() as conn:
        ids = dict(conn.execute(text(
            "SELECT title, id FROM canonical_jobs")).all())
        for title, (score, passed, days, p_hash, key) in specs.items():
            conn.execute(text(
                "UPDATE canonical_jobs SET last_seen_at = now() - make_interval("
                "days => :d) WHERE id = :id"), {"d": days, "id": ids[title]})
            conn.execute(text(
                "INSERT INTO job_scores (job_id, profile_hash, cache_key, content_hash, "
                "passed, match_score, score_breakdown, explanation) VALUES (:j, :p, :k, "
                "'c', :passed, :s, CAST(:b AS jsonb), CAST(:e AS jsonb))"),
                {"j": ids[title], "p": p_hash, "k": key, "passed": passed,
                 "s": score if passed else None,
                 "b": json.dumps({"semantic": 0.5}) if passed else None,
                 "e": json.dumps({"strong_matches": []}) if passed else None})
    return ids


def _spec(score, passed=True, days=0, p_hash=PROFILE_HASH, key=CACHE_KEY):
    return (score, passed, days, p_hash, key)


def _triage(user_id, cap=50, min_score=80, age=14):
    return store.triage(user_id=user_id, profile_hash=PROFILE_HASH,
                        cache_key=CACHE_KEY, min_score=min_score,
                        max_age_days=age, cap=cap)


@pytest.fixture
def user(pg, profile):
    return store.user_for_profile(profile)


def test_triage_applies_score_age_profile_and_cache_key_and_orders_by_score(pg, user):
    ids = _seed(pg, {
        "A": _spec(95), "B": _spec(90), "D": _spec(80),          # 80: inclusive
        "E": _spec(79.99),                                         # below threshold
        "F": _spec(None, passed=False),                            # gated out
        "G": _spec(99, days=15),                                   # too old
        "H": _spec(99, days=13),                                   # old but in window
        "I": _spec(97, p_hash="q" * 64),                           # another profile
        "J": _spec(97, key="1:000000000000"),                      # stale cache key
        "T1": _spec(85), "T2": _spec(85),                          # a tie
    })
    picked = _triage(user)
    assert [c["title"] for c in picked] == ["H", "A", "B", "T1", "T2", "D"]
    assert picked[0] == {"job_id": ids["H"], "match_score": 99.0, "title": "H",
                         "company": "acme"}
    assert ids["T1"] < ids["T2"]                                   # ties: job id


def test_triage_skips_jobs_with_an_application_or_a_closing_event(pg, user):
    from backend.database.events import log_outcome
    ids = _seed(pg, {t: _spec(90) for t in
                     ["applied", "dismissed", "declined", "saved", "drafted",
                      "has_app", "other_users_app", "untouched"]})
    for title, event in (("applied", "applied"), ("dismissed", "dismiss"),
                         ("declined", "declined"), ("saved", "save"),
                         ("drafted", "drafted")):
        log_outcome(user, "acme", title, event)
    store.dispatch(user_id=user, job_id=ids["has_app"], match_score=90,
                   profile_hash=PROFILE_HASH, cache_key=CACHE_KEY)
    other = store.user_for_profile({"email": "someone@else"})
    store.dispatch(user_id=other, job_id=ids["other_users_app"], match_score=90,
                   profile_hash=PROFILE_HASH, cache_key=CACHE_KEY)
    assert sorted(c["title"] for c in _triage(user)) == [
        "drafted", "other_users_app", "saved", "untouched"]


def test_triage_caps_the_batch(pg, user):
    _seed(pg, {f"J{i}": _spec(90 - i) for i in range(8)})
    assert [c["title"] for c in _triage(user, cap=3)] == ["J0", "J1", "J2"]
    assert _triage(user, cap=0) == []


# ---- dispatch -------------------------------------------------------------------------

def _counts(pg):
    with pg.connect() as conn:
        return (conn.execute(text("SELECT count(*) FROM applications")).scalar(),
                conn.execute(text("SELECT count(*) FROM tasks WHERE kind = 'application'"
                                  )).scalar())


def test_dispatch_writes_the_application_and_its_task_or_neither(pg, user, monkeypatch):
    ids = _seed(pg, {"A": _spec(90)})
    real_enqueue = taskqueue.enqueue

    def dies(*args, **kwargs):
        raise RuntimeError("worker died between the insert and the enqueue")
    monkeypatch.setattr(taskqueue, "enqueue", dies)
    with pytest.raises(RuntimeError):
        store.dispatch(user_id=user, job_id=ids["A"], match_score=90,
                       profile_hash=PROFILE_HASH, cache_key=CACHE_KEY)
    assert _counts(pg) == (0, 0)                                   # neither

    monkeypatch.setattr(taskqueue, "enqueue", real_enqueue)
    made = store.dispatch(user_id=user, job_id=ids["A"], match_score=90,
                          profile_hash=PROFILE_HASH, cache_key=CACHE_KEY)
    assert _counts(pg) == (1, 1)                                   # both
    task = taskqueue.get_task(made["task_id"])
    app = store.get_application(made["application_id"])
    assert task["payload"] == {"application_id": app["id"]}
    assert task["idempotency_key"] == f"application:{app['id']}"
    assert app["thread_id"] == f"application:{app['id']}"
    assert (app["status"], app["match_score"], app["cache_key"]) == ("queued", 90.0,
                                                                     CACHE_KEY)
    # Again for the same (user, job): nothing new.
    assert store.dispatch(user_id=user, job_id=ids["A"], match_score=90,
                          profile_hash=PROFILE_HASH, cache_key=CACHE_KEY) is None
    assert _counts(pg) == (1, 1)


# ---- the run graph, end to end (real rank, fake semantic score) ------------------------

def _corpus(n=4):
    corpus.upsert_jobs([{**_job(f"AI Engineer {i}"),
                         "clean_description": "Build RAG pipelines with FAISS, "
                                              "LLMs and Pandas. Git required."}
                        for i in range(n)])


def _run_agent(monkeypatch, **env):
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    task_id = run_graph.request_agent_run(trigger="api")
    result = Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                    lease_s=30).run_once()
    return task_id, result, taskqueue.get_task(task_id)


def test_a_run_refreshes_triages_and_dispatches(agent_env, profile, monkeypatch):
    _corpus(4)
    task_id, run, task = _run_agent(monkeypatch, AGENT_MAX_DRAFTS_PER_RUN=3)
    assert run.outcome == "succeeded", run.error
    result = task["result"]
    assert result["refresh"]["scored"] == 4                 # first rank: everything
    assert result["profile_hash"] == scoring.profile_hash(profile)
    assert result["cache_key"] == scoring.cache_key()
    assert (result["triaged"], len(result["dispatched"])) == (3, 3)
    scores = [d["match_score"] for d in result["dispatched"]]
    assert scores == sorted(scores, reverse=True)
    assert _counts(agent_env.pg) == (3, 3)
    assert agent_env.ranked_file.exists()                   # the rank published

    # A second run: the rank is a no-op, the 3 jobs are taken, 1 is left.
    _, run, task = _run_agent(monkeypatch, AGENT_MAX_DRAFTS_PER_RUN=3)
    assert task["result"]["refresh"]["scored"] == 0
    assert (task["result"]["triaged"], len(task["result"]["dispatched"])) == (1, 1)
    _, run, task = _run_agent(monkeypatch)
    assert task["result"]["triaged"] == 0
    assert _counts(agent_env.pg) == (4, 4)                  # never a duplicate


def test_a_run_that_dies_in_dispatch_resumes_without_duplicates(agent_env, monkeypatch):
    _corpus(3)
    real = store.dispatch
    calls = []

    def dies_on_the_second(**kwargs):
        calls.append(kwargs["job_id"])
        if len(calls) == 2:
            raise RuntimeError("worker died mid-dispatch")
        return real(**kwargs)
    monkeypatch.setattr(store, "dispatch", dies_on_the_second)
    task_id, run, task = _run_agent(monkeypatch)
    assert run.outcome == "failed" and task["status"] == "queued"
    assert _counts(agent_env.pg) == (1, 1)

    monkeypatch.setattr(store, "dispatch", real)
    with agent_env.pg.begin() as conn:
        conn.execute(text("UPDATE tasks SET run_after = now()"))
    run = Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                 lease_s=30).run_once()
    task = taskqueue.get_task(task_id)
    assert run.outcome == "succeeded" and task["attempts"] == 2
    # The same thread resumed at dispatch: triage was not redone, the first
    # job's application was found, the other two were created.
    assert task["result"]["triaged"] == 3
    assert task["result"]["skipped_existing"] == 1
    assert len(task["result"]["dispatched"]) == 2
    assert _counts(agent_env.pg) == (3, 3)


def test_without_a_profile_the_run_is_skipped(agent_env, monkeypatch):
    agent_env.profile_file.unlink()
    _corpus(2)
    _, run, task = _run_agent(monkeypatch)
    assert run.outcome == "succeeded"
    assert task["result"]["skipped"].startswith("no candidate profile")
    assert _counts(agent_env.pg) == (0, 0)


def test_one_run_at_a_time(agent_env):
    run_graph.request_agent_run(trigger="api")
    with pytest.raises(taskqueue.ConcurrencyConflict):
        run_graph.request_agent_run(trigger="api")
