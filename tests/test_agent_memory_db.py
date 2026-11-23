"""Agent memory against a real Postgres (backend/agent/memory.py): learning
from approvals rows, stated memories that learning never overwrites, and
triage, where *_avoid excludes and *_prefer only reorders (the scores,
the score gate and job_scores never change)."""
import json

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("langgraph.checkpoint.postgres")

from sqlalchemy import text                       # noqa: E402

from backend import taskqueue                     # noqa: E402
from backend.agent import memory, run_graph, store  # noqa: E402
from backend.database import corpus               # noqa: E402
from backend.worker import Worker                 # noqa: E402

pytestmark = pytest.mark.db

PROFILE_HASH, CACHE_KEY = "p" * 64, "1:abcdef012345"


def _job(title, company="acme", location="Bengaluru, India"):
    return {"title": title, "company": company, "location": location, "tags": [],
            "experience": "1 years", "seniority": "Junior",
            "clean_description": f"{title}: RAG with FAISS.",
            "apply_link": f"https://x/{company}/{title}", "source": "ats"}


def _seed(pg, jobs, scores):
    """jobs: [job]; scores: {(company, title): score}. Returns {(company, title): id}."""
    corpus.upsert_jobs(jobs)
    with pg.begin() as conn:
        ids = {(c, t): i for i, c, t in conn.execute(text(
            "SELECT id, company, title FROM canonical_jobs")).all()}
        for key, score in scores.items():
            conn.execute(text(
                "INSERT INTO job_scores (job_id, profile_hash, cache_key, content_hash, "
                "passed, match_score, score_breakdown, explanation) VALUES (:j, :p, :k, "
                "'c', true, :s, CAST(:b AS jsonb), CAST(:e AS jsonb))"),
                {"j": ids[key], "p": PROFILE_HASH, "k": CACHE_KEY, "s": score,
                 "b": json.dumps({"semantic": 0.5}), "e": json.dumps({"strong_matches": []})})
    return ids


@pytest.fixture
def user(pg, profile):
    return store.user_for_profile(profile)


def _decide(pg, user, job_id, decision, reason=None):
    """An application for the job, decided by the user: its approvals row
    and the event the decision task would log."""
    made = store.dispatch(user_id=user, job_id=job_id, match_score=50,
                          profile_hash=PROFILE_HASH, cache_key=CACHE_KEY)
    app_id = made["application_id"]
    with pg.begin() as conn:
        conn.execute(text("UPDATE applications SET status = :s, version = 1 WHERE id = :a"),
                     {"s": "declined" if decision == "decline" else "ready_to_apply",
                      "a": app_id})
        approval = conn.execute(text(
            "INSERT INTO approvals (application_id, decision, reason, source, "
            "application_version) VALUES (:a, :d, :r, 'user', 1) RETURNING id"),
            {"a": app_id, "d": decision, "r": reason}).scalar()
        event = conn.execute(text(
            "INSERT INTO events (user_id, job_id, event_type, source) VALUES "
            "(:u, :j, :t, 'user') RETURNING id"),
            {"u": user, "j": job_id,
             "t": "declined" if decision == "decline" else "approved"}).scalar()
    return {"application_id": app_id, "approval_id": approval, "event_id": event}


def _learned(user):
    return {(m["kind"], m["value"].lower()): m for m in memory.list_memories(user)}


def _scores(pg):
    with pg.connect() as conn:
        return conn.execute(text(
            "SELECT job_id, profile_hash, cache_key, content_hash, passed, match_score, "
            "score_breakdown, explanation, scored_at FROM job_scores ORDER BY job_id")).all()


# ---- learning ------------------------------------------------------------------------

def test_a_company_declined_twice_is_learned_with_its_evidence(pg, user):
    ids = _seed(pg, [_job(f"Role {i}", company="acme") for i in range(3)], {})
    first = _decide(pg, user, ids[("acme", "Role 0")], "decline", reason="too far")
    assert memory.learn(user)["created"] == 0
    assert ("company_avoid", "acme") not in _learned(user)
    second = _decide(pg, user, ids[("acme", "Role 1")], "decline")
    assert memory.learn(user) == {"created": 1, "updated": 0, "removed": 0, "learned": 1}
    m = _learned(user)[("company_avoid", "acme")]
    assert (m["provenance"], m["active"], m["weight"]) == ("learned", True, 2.0)
    declined = m["evidence"]["declined"]
    assert [e["approval_id"] for e in declined] == [first["approval_id"],
                                                    second["approval_id"]]
    assert [e["event_id"] for e in declined] == [first["event_id"], second["event_id"]]
    assert m["evidence"]["decline_reasons"] == ["too far"]
    # Idempotent: the same decisions change nothing.
    assert memory.learn(user) == {"created": 0, "updated": 0, "removed": 0, "learned": 1}

    # An approval for that company withdraws it.
    _decide(pg, user, ids[("acme", "Role 2")], "approve")
    assert memory.learn(user)["removed"] == 1
    assert ("company_avoid", "acme") not in _learned(user)


def test_a_title_word_needs_three_declines_and_no_approval(pg, user):
    titles = ["Frontend Engineer", "Frontend Developer", "Senior Frontend Engineer"]
    ids = _seed(pg, [_job(t, company=f"c{i}") for i, t in enumerate(titles)], {})
    for i, t in enumerate(titles[:2]):
        _decide(pg, user, ids[(f"c{i}", t)], "decline")
    memory.learn(user)
    assert ("title_avoid", "frontend") not in _learned(user)
    _decide(pg, user, ids[("c2", titles[2])], "decline")
    memory.learn(user)
    learned = _learned(user)
    assert learned[("title_avoid", "frontend")]["weight"] == 3.0
    assert ("title_avoid", "engineer") not in learned          # generic: never learned


def test_approvals_learn_prefer_memories(pg, user):
    ids = _seed(pg, [_job("ML Engineer", company="gitlab", location="Remote, India"),
                     _job("ML Platform Engineer", company="gitlab",
                          location="Remote - India")], {})
    for key in ids:
        _decide(pg, user, ids[key], "approve")
    memory.learn(user)
    learned = _learned(user)
    assert learned[("company_prefer", "gitlab")]["weight"] == 2.0
    assert learned[("title_prefer", "ml")]["weight"] == 2.0
    assert learned[("location_prefer", "remote")]["weight"] == 2.0
    assert ("title_prefer", "platform") not in learned


def test_thresholds_are_configurable(pg, user, monkeypatch):
    ids = _seed(pg, [_job("Data Analyst", company="acme")], {})
    _decide(pg, user, ids[("acme", "Data Analyst")], "decline")
    monkeypatch.setenv("AGENT_LEARN_COMPANY_DECLINES", "1")
    memory.learn(user)
    assert ("company_avoid", "acme") in _learned(user)


def test_expiries_and_edits_teach_nothing(pg, user):
    ids = _seed(pg, [_job(f"Role {i}") for i in range(3)], {})
    for i, decision in enumerate(("expire", "expire", "edit")):
        made = store.dispatch(user_id=user, job_id=ids[("acme", f"Role {i}")],
                              match_score=50, profile_hash=PROFILE_HASH, cache_key=CACHE_KEY)
        with pg.begin() as conn:
            conn.execute(text(
                "INSERT INTO approvals (application_id, decision, source, "
                "application_version) VALUES (:a, :d, :s, 1)"),
                {"a": made["application_id"], "d": decision,
                 "s": "expiry" if decision == "expire" else "user"})
    assert memory.learn(user)["learned"] == 0


def test_a_stated_memory_is_never_overwritten_by_learning(pg, user):
    ids = _seed(pg, [_job(f"Role {i}") for i in range(2)], {})
    stated = memory.create_stated(user, kind="company_avoid", value="ACME", weight=7)
    for key in ids:
        _decide(pg, user, ids[key], "decline")
    memory.learn(user)
    [m] = memory.list_memories(user, kind="company_avoid")
    assert (m["id"], m["provenance"], m["value"], m["weight"]) == (
        stated["id"], "stated", "ACME", 7.0)
    assert m["evidence"] == {"stated_via": "api"}
    assert m["updated_at"] == stated["updated_at"]


def test_learning_never_contradicts_a_stated_preference(pg, user):
    ids = _seed(pg, [_job(f"Role {i}") for i in range(2)], {})
    memory.create_stated(user, kind="company_prefer", value="acme")
    for key in ids:
        _decide(pg, user, ids[key], "decline")
    memory.learn(user)
    assert ("company_avoid", "acme") not in _learned(user)


def test_stating_a_learned_memory_makes_it_stated(pg, user):
    ids = _seed(pg, [_job(f"Role {i}") for i in range(2)], {})
    for key in ids:
        _decide(pg, user, ids[key], "decline")
    memory.learn(user)
    learned = _learned(user)[("company_avoid", "acme")]
    stated = memory.create_stated(user, kind="company_avoid", value="Acme", via="ui")
    assert stated["id"] == learned["id"] and stated["provenance"] == "stated"
    assert stated["evidence"]["stated_via"] == "ui"
    assert len(stated["evidence"]["declined"]) == 2            # the evidence is kept
    with pytest.raises(memory.MemoryConflict):
        memory.create_stated(user, kind="company_avoid", value="acme")


def test_a_deactivated_learned_memory_is_frozen(pg, user):
    ids = _seed(pg, [_job(f"Role {i}") for i in range(4)], {})
    for i in range(2):
        _decide(pg, user, ids[("acme", f"Role {i}")], "decline")
    memory.learn(user)
    m = _learned(user)[("company_avoid", "acme")]
    off = memory.update(user, m["id"], active=False)
    assert (off["active"], off["provenance"]) == (False, "learned")
    _decide(pg, user, ids[("acme", "Role 2")], "decline")      # more evidence
    memory.learn(user)
    again = memory.get_memory(user, m["id"])
    assert (again["active"], again["weight"]) == (False, 2.0)  # not re-weighted
    _decide(pg, user, ids[("acme", "Role 3")], "approve")      # evidence gone
    memory.learn(user)
    assert memory.get_memory(user, m["id"]) is not None        # not removed either


def test_editing_a_learned_memory_makes_it_stated(pg, user):
    ids = _seed(pg, [_job(f"Role {i}") for i in range(2)], {})
    for key in ids:
        _decide(pg, user, ids[key], "decline")
    memory.learn(user)
    m = _learned(user)[("company_avoid", "acme")]
    edited = memory.update(user, m["id"], weight=9)
    assert (edited["provenance"], edited["weight"]) == ("stated", 9.0)
    with pytest.raises(memory.MemoryNotFound):
        memory.update(user, 999999, active=False)
    with pytest.raises(memory.InvalidMemory):
        memory.update(user, m["id"])
    with pytest.raises(memory.InvalidMemory):
        memory.update(user, m["id"], weight=-1)
    other = memory.create_stated(user, kind="company_avoid", value="other")
    with pytest.raises(memory.MemoryConflict):
        memory.update(user, other["id"], value="ACME")
    assert memory.delete(user, other["id"]) is True
    assert memory.delete(user, other["id"]) is False


def test_stated_memories_are_validated(pg, user):
    with pytest.raises(memory.InvalidMemory):
        memory.create_stated(user, kind="salary_floor", value="x")
    with pytest.raises(memory.InvalidMemory):
        memory.create_stated(user, kind="title_avoid", value="  ")
    with pytest.raises(memory.InvalidMemory):
        memory.create_stated(user, kind="title_avoid", value="--")
    note = memory.create_stated(user, kind="note", value="  Ask   about visas. ")
    assert note["value"] == "Ask about visas." and note["weight"] == 2.0


# ---- triage ---------------------------------------------------------------------------

def _triage(user, memories=None, min_score=20, cap=50, stats=None):
    return store.triage(user_id=user, profile_hash=PROFILE_HASH, cache_key=CACHE_KEY,
                        min_score=min_score, max_age_days=14, cap=cap,
                        memories=memories, stats=stats)


def _set(*specs, max_boost=5.0):
    return memory.MemorySet([{"id": i, "kind": k, "value": v, "weight": w, "active": a,
                              "provenance": "stated"}
                             for i, (k, v, w, a) in enumerate(specs, 1)],
                            max_boost=max_boost)


def test_avoid_memories_exclude_and_are_counted(pg, user):
    _seed(pg, [_job("AI Engineer", company="acme"), _job("Frontend Engineer", company="b"),
               _job("ML Engineer", company="c")],
          {("acme", "AI Engineer"): 30, ("b", "Frontend Engineer"): 29,
           ("c", "ML Engineer"): 28})
    stats = {}
    picked = _triage(user, _set(("company_avoid", "ACME", 1, True),
                                ("title_avoid", "frontend", 1, True)), stats=stats)
    assert [c["title"] for c in picked] == ["ML Engineer"]
    assert stats == {"location": 0, "title": 0, "memory": 2}


def test_prefer_memories_reorder_only(pg, user):
    _seed(pg, [_job("AI Engineer", company="top"), _job("ML Engineer", company="gitlab"),
               _job("Data Engineer", company="gitlab2"), _job("CV Engineer", company="gitlab")],
          {("top", "AI Engineer"): 30, ("gitlab", "ML Engineer"): 27,
           ("gitlab2", "Data Engineer"): 29, ("gitlab", "CV Engineer"): 19.9})
    before = _scores(pg)
    plain = _triage(user)
    assert [c["title"] for c in plain] == ["AI Engineer", "Data Engineer", "ML Engineer"]
    boosted = _triage(user, _set(("company_prefer", "gitlab", 4, True)))
    # 27 + 4 = 31 moves ahead of 30 and 29; its match_score is still 27.
    assert [c["title"] for c in boosted] == ["ML Engineer", "AI Engineer", "Data Engineer"]
    ml = boosted[0]
    assert ml["match_score"] == 27.0
    assert ml["memory_effects"] == {"boost": 4.0, "memories": [
        {"id": 1, "kind": "company_prefer", "value": "gitlab", "provenance": "stated",
         "weight": 4.0}]}
    assert boosted[1]["memory_effects"] is None
    # The gate holds: CV Engineer (19.9 < 20) is preferred too, but never triaged.
    assert "CV Engineer" not in [c["title"] for c in boosted]
    # The cap applies after the reordering.
    assert [c["title"] for c in _triage(user, _set(("company_prefer", "gitlab", 4, True)),
                                        cap=1)] == ["ML Engineer"]
    assert _scores(pg) == before                               # job_scores untouched


def test_the_boost_is_capped_and_ties_keep_score_order(pg, user):
    _seed(pg, [_job("AI Engineer", company="a"), _job("ML Engineer", company="b")],
          {("a", "AI Engineer"): 30, ("b", "ML Engineer"): 20})
    big = _set(("company_prefer", "b", 50, True), ("title_prefer", "ml", 50, True),
               max_boost=5)
    picked = _triage(user, big)
    assert [c["title"] for c in picked] == ["AI Engineer", "ML Engineer"]   # 20 + 5 < 30
    assert picked[1]["memory_effects"]["boost"] == 5.0
    tie = _set(("company_prefer", "b", 10, True), max_boost=10)            # 20 + 10 == 30
    assert [c["title"] for c in _triage(user, tie)] == ["AI Engineer", "ML Engineer"]


def test_a_deactivated_memory_has_no_effect(pg, user):
    _seed(pg, [_job("AI Engineer", company="acme"), _job("ML Engineer", company="b")],
          {("acme", "AI Engineer"): 30, ("b", "ML Engineer"): 25})
    off = _set(("company_avoid", "acme", 1, False), ("company_prefer", "b", 10, False))
    stats = {}
    picked = _triage(user, off, stats=stats)
    assert [c["title"] for c in picked] == ["AI Engineer", "ML Engineer"]
    assert all(c["memory_effects"] is None for c in picked) and stats["memory"] == 0
    # ...and through the database: a deactivated stated memory is not loaded.
    m = memory.create_stated(user, kind="company_avoid", value="acme")
    memory.update(user, m["id"], active=False)
    assert len(memory.MemorySet.load(user, max_boost=5)) == 0


def test_dispatch_records_the_memory_effects(pg, user):
    ids = _seed(pg, [_job("ML Engineer", company="gitlab")], {("gitlab", "ML Engineer"): 27})
    [c] = _triage(user, _set(("company_prefer", "gitlab", 2, True)))
    made = store.dispatch(user_id=user, job_id=ids[("gitlab", "ML Engineer")],
                          match_score=c["match_score"], profile_hash=PROFILE_HASH,
                          cache_key=CACHE_KEY, memory_effects=c["memory_effects"])
    app = store.get_application(made["application_id"])
    assert app["match_score"] == 27.0
    assert app["memory_effects"]["boost"] == 2.0


# ---- end to end: the run graph learns, then triages with the memories -------------------

def test_an_agent_run_learns_and_applies_memories(agent_env, monkeypatch):
    jd = "Build RAG pipelines with FAISS and LLMs."
    corpus.upsert_jobs([{**_job("AI Engineer", company="acme"), "clean_description": jd},
                        {**_job("AI Engineer 2", company="acme"), "clean_description": jd},
                        {**_job("AI Engineer 3", company="acme"), "clean_description": jd},
                        {**_job("AI Engineer", company="other"), "clean_description": jd}])
    user = store.user_for_profile(_profile_of(agent_env))
    with agent_env.pg.begin() as conn:
        ids = {(c, t): i for i, c, t in conn.execute(text(
            "SELECT id, company, title FROM canonical_jobs")).all()}
    for key in (("acme", "AI Engineer"), ("acme", "AI Engineer 2")):
        _decide(agent_env.pg, user, ids[key], "decline", reason="not this company")
    before = memory.list_memories(user)
    assert before == []
    monkeypatch.setenv("AGENT_MIN_SCORE", "0")
    task_id = run_graph.request_agent_run(trigger="api")
    run = Worker({run_graph.AGENT_RUN_KIND: run_graph.run_agent_run_task},
                 lease_s=30).run_once()
    assert run.outcome == "succeeded", run.error
    result = taskqueue.get_task(task_id)["result"]
    assert result["memory"]["learned"]["created"] >= 1
    assert result["excluded"]["memory"] == 1                    # "AI Engineer 3" at acme
    assert [(d["company"], d["title"]) for d in result["dispatched"]] == [
        ("other", "AI Engineer")]
    [m] = memory.list_memories(user, kind="company_avoid")
    assert (m["value"], m["provenance"]) == ("acme", "learned")


def _profile_of(env):
    return json.loads(env.profile_file.read_text(encoding="utf-8"))
