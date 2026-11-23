"""The board probe (backend/job_scraper/probe_boards.py) with the HTTP layer
mocked: a board is kept only if it answers 200 and has a posting in
AGENT_LOCATIONS; no-India boards, 404s and network errors are dropped and
say why; only the three public job-board API hosts are ever called."""
import json

import pytest

pytest.importorskip("requests")
pytest.importorskip("bs4")                         # job_parser cleans HTML

import requests                                    # noqa: E402

from backend.agent.locations import LocationPolicy  # noqa: E402
from backend.job_scraper import ats_collector, probe_boards  # noqa: E402

HOSTS = ("https://boards-api.greenhouse.io/", "https://api.lever.co/",
         "https://api.ashbyhq.com/")


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


def _gh(*locations):
    return {"jobs": [{"id": 100 + i, "title": f"ML Engineer {i}",
                      "location": {"name": loc}, "departments": [],
                      "content": "<p>Models.</p>",
                      "absolute_url": f"https://boards.greenhouse.io/x/jobs/{100 + i}"}
                     for i, loc in enumerate(locations)]}


@pytest.fixture
def web(monkeypatch):
    """routes: url (without query) -> FakeResponse or an exception to raise.
    Anything unrouted is a 404. Every call is recorded."""
    routes, calls = {}, []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        assert url.startswith(HOSTS), f"probe called a non-ATS URL: {url}"
        answer = routes.get(url, FakeResponse(404, {"error": "not found"}))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(ats_collector.requests, "get", fake_get)
    monkeypatch.setattr(ats_collector, "RETRY_BACKOFF_SECONDS", 0)
    for var in ("AGENT_LOCATIONS", "AGENT_ACCEPT_GLOBAL_REMOTE"):
        monkeypatch.delenv(var, raising=False)
    return routes, calls


GH = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
GH_META = "https://boards-api.greenhouse.io/v1/boards/{}"
LEVER = "https://api.lever.co/v0/postings/{}"
ASHBY = "https://api.ashbyhq.com/posting-api/job-board/{}"


def _probe(name, slugs=None):
    return probe_boards.probe_company(name, slugs, policy=LocationPolicy(), delay_s=0)


def test_a_200_board_with_an_india_posting_is_kept(web):
    routes, _ = web
    routes[GH.format("goodco")] = FakeResponse(200, _gh("San Francisco", "Bengaluru, India"))
    routes[GH_META.format("goodco")] = FakeResponse(200, {"name": "GoodCo"})
    result = _probe("GoodCo")
    [kept] = result.kept
    assert (kept.ats, kept.slug, kept.status) == ("greenhouse", "goodco", "kept")
    assert (kept.total, kept.eligible, kept.board_name) == (2, 1, "GoodCo")
    assert kept.sample == {"title": "ML Engineer 1", "location": "Bengaluru, India",
                           "apply_link": "https://boards.greenhouse.io/x/jobs/101"}
    assert result.reason is None


def test_a_200_board_without_an_india_posting_is_dropped(web):
    routes, _ = web
    routes[GH.format("usonly")] = FakeResponse(200, _gh("San Francisco", "Remote - USA"))
    result = _probe("UsOnly")
    assert result.kept == []
    assert [a.status for a in result.attempts if a.slug == "usonly"] == [
        "no_eligible", "not_found", "not_found"]
    assert "greenhouse/usonly (2 postings)" in result.reason
    assert "no posting in AGENT_LOCATIONS" in result.reason


def test_a_404_everywhere_is_dropped(web):
    _, calls = web
    result = _probe("Missing Co")
    assert result.kept == []
    assert {a.status for a in result.attempts} == {"not_found"}
    # 9 slugs x 3 ATS, each tried once (a 404 is not retried)
    assert len(result.attempts) == len(calls) == 27
    assert result.reason.startswith("no public board under missingco, missing-co, missing")
    assert "HTTP 404 for all 27" in result.reason


def test_a_network_error_is_dropped_and_reported(web):
    routes, calls = web
    routes[LEVER.format("flaky")] = requests.ConnectionError("connection reset")
    result = _probe("Flaky", ["flaky"])
    assert result.kept == []
    [lever] = [a for a in result.attempts if a.ats == "lever"]
    assert lever.status == "error" and "ConnectionError" in lever.error
    assert "no usable answer from lever/flaky" in result.reason
    assert calls.count(LEVER.format("flaky")) == ats_collector.RETRY_COUNT + 1  # retried


def test_an_unexpected_payload_is_an_error_not_a_board(web):
    routes, _ = web
    routes[ASHBY.format("weird")] = FakeResponse(200, {"unexpected": True})
    result = _probe("Weird", ["weird"])
    [ashby] = [a for a in result.attempts if a.ats == "ashby"]
    assert ashby.status == "error" and "no 'jobs' list" in ashby.error
    assert result.kept == []


def test_lever_and_ashby_boards_are_kept_too_and_each_ats_stops_at_its_first_keep(web):
    routes, calls = web
    routes[LEVER.format("multi")] = FakeResponse(200, [
        {"id": "l1", "text": "Data Engineer", "categories": {"location": "Pune"},
         "descriptionPlain": "x", "hostedUrl": "https://jobs.lever.co/multi/l1"}])
    routes[ASHBY.format("multiindia")] = FakeResponse(200, {"jobs": [
        {"id": "a1", "title": "AI Engineer", "location": "Remote - India",
         "descriptionPlain": "x", "jobUrl": "https://jobs.ashbyhq.com/multiindia/a1"}]})
    result = _probe("Multi")
    assert [(k.ats, k.slug, k.eligible) for k in result.kept] == [
        ("lever", "multi", 1), ("ashby", "multiindia", 1)]
    assert LEVER.format("multiindia") not in calls          # lever stopped at "multi"


def test_candidate_slugs():
    assert probe_boards.candidate_slugs("Razorpay") == ["razorpay", "razorpayindia",
                                                        "razorpayhq"]
    assert probe_boards.candidate_slugs("Sarvam AI") == [
        "sarvamai", "sarvam-ai", "sarvam", "sarvamaiindia", "sarvam-ai-india",
        "sarvamindia", "sarvamaihq", "sarvam-ai-hq", "sarvamhq"]
    assert probe_boards.candidate_slugs("Observe.AI")[:3] == ["observeai", "observe-ai",
                                                             "observe"]
    assert probe_boards.candidate_slugs("CRED")[0] == "cred"
    assert probe_boards.candidate_slugs("!!") == []


def test_explicit_slugs():
    assert probe_boards.parse_target("Fi=epifi, fimoney") == ("Fi", ["epifi", "fimoney"])
    assert probe_boards.parse_target("Zepto") == ("Zepto", ["zepto", "zeptoindia",
                                                            "zeptohq"])
    with pytest.raises(ValueError):
        probe_boards.parse_target("Fi=bad slug!")
    with pytest.raises(ValueError):
        probe_boards.parse_target("=x")


def test_the_cli_prints_the_table_and_writes_json(web, tmp_path, capsys):
    routes, _ = web
    routes[GH.format("goodco")] = FakeResponse(200, _gh("Hyderabad"))
    out = tmp_path / "probe.json"
    assert probe_boards.main(["GoodCo=goodco", "Gone=gone", "--delay", "0",
                              "--json", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "| GoodCo | greenhouse | goodco | - | 1 | 1 | ML Engineer 0 (Hyderabad) |" in printed
    assert "- Gone: no public board under gone" in printed
    assert "GREENHOUSE_COMPANIES: 'goodco'" in printed
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [d["company"] for d in data] == ["GoodCo", "Gone"]
    assert data[0]["kept"][0]["eligible"] == 1 and data[1]["kept"] == []


def test_the_probe_honours_agent_locations(web, monkeypatch):
    routes, _ = web
    routes[GH.format("berlin")] = FakeResponse(200, _gh("Berlin, Germany"))
    monkeypatch.setenv("AGENT_LOCATIONS", "Berlin")
    assert [k.slug for k in probe_boards.probe_company(
        "Berlin", ["berlin"], delay_s=0, ats=("greenhouse",)).kept] == ["berlin"]
