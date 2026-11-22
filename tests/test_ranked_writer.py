"""data/ranked_jobs.json has one writer path: services.ranking (audit M7).
The worker, the DB-less demo and the CLIs all publish through it; nothing
else in backend/ may write RANKED_FILE. No DB, no ML stack."""
import json
import re
import sys
import types
from pathlib import Path

import pytest

from backend.services import ranking

BACKEND = Path(__file__).resolve().parents[1] / "backend"
WRITER = BACKEND / "services" / "ranking.py"

# open(RANKED_FILE, "w"...) / write_json_atomic(RANKED_FILE, ...) /
# RANKED_FILE.write_text(...) -- every way the codebase writes a file today.
_WRITES = re.compile(
    r"open\(\s*RANKED_FILE\s*,\s*[\"'][wax]"
    r"|write_json_atomic\(\s*RANKED_FILE"
    r"|RANKED_FILE\.write_(?:text|bytes)\(")


def test_only_the_ranking_service_writes_the_ranked_file():
    offenders = []
    for path in BACKEND.rglob("*.py"):
        if "node_modules" in path.parts or path == WRITER:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        offenders += [f"{path.relative_to(BACKEND)}: {m.group(0)}"
                      for m in _WRITES.finditer(text)]
    assert offenders == []
    assert _WRITES.search(WRITER.read_text(encoding="utf-8"))   # the pattern works


def test_rank_and_publish_writes_through_the_single_writer(tmp_path, monkeypatch,
                                                           profile):
    ranked_file = tmp_path / "ranked_jobs.json"
    monkeypatch.setattr(ranking, "RANKED_FILE", ranked_file)
    monkeypatch.setattr(ranking, "_rank_jobs", lambda p, jobs: [
        {"title": j["title"], "match_score": 50.0} for j in jobs])
    out = ranking.rank_and_publish(profile, [{"title": "A"}, {"title": "B"}])
    assert json.loads(ranked_file.read_text(encoding="utf-8")) == out
    assert [j["title"] for j in out] == ["A", "B"]


def test_run_real_ranking_cli_publishes_through_the_service(tmp_path, monkeypatch,
                                                            profile, capsys):
    # The CLI imports the ranking service, not the matcher; stub the matcher
    # module anyway so this can't reach sentence-transformers by accident.
    fake = types.ModuleType("backend.matching_engine.matcher")

    def bypass(*args, **kwargs):
        pytest.fail("the CLI ranked around the service")
    fake.rank_jobs = bypass
    monkeypatch.setitem(sys.modules, "backend.matching_engine.matcher", fake)
    from backend.job_scraper import run_real_ranking as cli

    (tmp_path / "jobs.json").write_text(json.dumps([{"title": "AI Engineer"}]),
                                        encoding="utf-8")
    profile_file = tmp_path / "candidate_profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "PROFILE_FILE", profile_file)
    published = []
    monkeypatch.setattr(cli, "rank_and_publish", lambda p, jobs: published.append(
        jobs) or [{"title": "AI Engineer", "company": "acme", "match_score": 70.0,
                   "score_breakdown": {"skill_overlap": 0.5, "semantic": 0.5,
                                       "role": 1.0, "seniority": 1.0},
                   "strong_matches": [], "likely_matches": []}])
    cli.main()
    assert published == [[{"title": "AI Engineer"}]]
    assert "Saved ranked list" in capsys.readouterr().out

