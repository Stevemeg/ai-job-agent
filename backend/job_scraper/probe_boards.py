"""
probe_boards.py -- find a company's public job board before adding it to
the collector (Phase 6 sourcing).

    python -m backend.job_scraper.probe_boards Razorpay CRED "Sarvam AI" ...
    python -m backend.job_scraper.probe_boards "Fi=epifi" --json probe.json

Only the three documented public job-board APIs the collector already
reads (Greenhouse, Lever, Ashby; ADR-001 guardrail 3), through the
collector's own fetchers -- so a board this keeps is one the scan can
parse. No scraping, no search engine, no headless browser.

For each name it tries a few plausible slugs (candidate_slugs(): the name
joined, hyphenated, its first word, and those with "india" / "hq"
suffixes), or exactly the slugs given as "Name=slug1,slug2". A board is
KEPT only if it answers HTTP 200 AND currently has at least one posting
that AGENT_LOCATIONS accepts (agent/locations.py, the rules triage uses;
AGENT_ACCEPT_GLOBAL_REMOTE is honoured). Everything else is DROPPED and
reported with its reason: no board under any tried slug (404s), boards
that answer with no eligible posting, or a network error / unexpected
response. Nothing is guessed into the source list and nothing is written:
adding a kept board to ats_collector.py is a reviewed code change, and
the output prints the lines to add.

A 200 under a short or generic slug can be a different company's board,
so every kept board is shown with its own name (Greenhouse's board name)
and a sample eligible posting to check before adding it.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field

from . import ats_collector

ATS_ORDER = ("greenhouse", "lever", "ashby")
SUFFIXES = ("", "india", "hq")
DEFAULT_DELAY_S = 0.25

KEPT, NO_ELIGIBLE, NOT_FOUND, ERROR = "kept", "no_eligible", "not_found", "error"


@dataclass
class BoardProbe:
    ats: str
    slug: str
    status: str                       # kept | no_eligible | not_found | error
    total: int = 0                    # postings on the board right now
    eligible: int = 0                 # ...that AGENT_LOCATIONS accepts
    board_name: str | None = None     # the board's own name (Greenhouse)
    sample: dict | None = None        # one eligible posting: title, location, apply_link
    error: str | None = None


@dataclass
class CompanyProbe:
    company: str
    slugs: list[str]
    kept: list[BoardProbe] = field(default_factory=list)
    attempts: list[BoardProbe] = field(default_factory=list)

    @property
    def reason(self) -> str | None:
        """Why nothing was kept (None if something was)."""
        if self.kept:
            return None
        answered = [a for a in self.attempts if a.status == NO_ELIGIBLE]
        errors = [a for a in self.attempts if a.status == ERROR]
        parts = []
        if answered:
            parts.append("board(s) answer but have no posting in AGENT_LOCATIONS: "
                         + ", ".join(f"{a.ats}/{a.slug} ({a.total} postings)"
                                     for a in answered))
        if errors:
            parts.append("no usable answer from "
                         + ", ".join(f"{a.ats}/{a.slug} ({a.error})" for a in errors))
        if not parts:
            parts.append(f"no public board under {', '.join(self.slugs)} on "
                         f"{'/'.join(ATS_ORDER)} (HTTP 404 for all "
                         f"{len(self.attempts)})")
        return "; ".join(parts)


def candidate_slugs(name: str) -> list[str]:
    """The slugs worth trying for a company name, most specific first:
    "Sarvam AI" -> sarvamai, sarvam-ai, sarvam, then each with an "india"
    and an "hq" suffix. Lower-case letters and digits only (dots and
    other punctuation split words: "Observe.AI" -> observeai, ...)."""
    words = re.findall(r"[a-z0-9]+", name.casefold())
    if not words:
        return []
    bases = ["".join(words), "-".join(words)]
    if len(words) > 1:
        bases.append(words[0])
    out: list[str] = []
    for suffix in SUFFIXES:
        for base in bases:
            slug = base + suffix if "-" not in base or not suffix else f"{base}-{suffix}"
            if slug not in out:
                out.append(slug)
    return out


def parse_target(arg: str) -> tuple[str, list[str]]:
    """'Name' -> (Name, candidate_slugs(Name)); 'Name=a,b' -> (Name, [a, b])."""
    name, sep, slugs = arg.partition("=")
    name = name.strip()
    if not name:
        raise ValueError(f"empty company name in {arg!r}")
    if not sep:
        return name, candidate_slugs(name)
    given = [s.strip().lower() for s in slugs.split(",") if s.strip()]
    if not given or any(not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", s) for s in given):
        raise ValueError(f"bad slug list in {arg!r} (letters, digits, . _ -)")
    return name, given


def _policy():
    from ..agent.settings import settings_from_env
    return settings_from_env().location_policy()


def _board_name(ats: str, slug: str) -> str | None:
    if ats != "greenhouse":
        return None                   # Lever's and Ashby's board APIs carry no name
    try:
        meta = ats_collector._fetch_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}")
    except ats_collector.SourceFetchError:
        return None
    return meta.get("name") if isinstance(meta, dict) else None


def probe_board(ats: str, slug: str, policy=None) -> BoardProbe:
    """One (ats, slug): fetched with the collector's strict fetcher, and
    its postings checked against the location policy."""
    from ..agent.locations import job_eligibility
    policy = policy or _policy()
    try:
        with contextlib.redirect_stdout(io.StringIO()):     # the fetchers print
            jobs = ats_collector.FETCHERS[ats](slug, strict=True)
    except ats_collector.SourceFetchError as exc:
        status = NOT_FOUND if "HTTP 404" in str(exc) else ERROR
        return BoardProbe(ats, slug, status, error=None if status == NOT_FOUND else str(exc))
    except Exception as exc:                                # noqa: BLE001
        return BoardProbe(ats, slug, ERROR, error=f"{exc.__class__.__name__}: {exc}")
    eligible = [j for j in jobs
                if job_eligibility(j.get("location"), j.get("postings"), policy).eligible]
    if not eligible:
        return BoardProbe(ats, slug, NO_ELIGIBLE, total=len(jobs))
    first = eligible[0]
    return BoardProbe(ats, slug, KEPT, total=len(jobs), eligible=len(eligible),
                      board_name=_board_name(ats, slug),
                      sample={"title": first.get("title"), "location": first.get("location"),
                              "apply_link": first.get("apply_link")})


def probe_company(name: str, slugs: list[str] | None = None, *, policy=None,
                  delay_s: float = DEFAULT_DELAY_S, ats=ATS_ORDER) -> CompanyProbe:
    """Try every slug on every ATS; per ATS, stop at the first slug that is
    kept. A 200 with no eligible posting does not stop the search (the
    plain name can belong to someone else)."""
    policy = policy or _policy()
    slugs = candidate_slugs(name) if slugs is None else slugs
    result = CompanyProbe(company=name, slugs=list(slugs))
    for source in ats:
        for slug in slugs:
            probe = probe_board(source, slug, policy)
            result.attempts.append(probe)
            if delay_s:
                time.sleep(delay_s)
            if probe.status == KEPT:
                result.kept.append(probe)
                break
    return result


# --------------------------------------------------------------------------
# the CLI
# --------------------------------------------------------------------------
def report(results: list[CompanyProbe]) -> str:
    lines = ["Kept (HTTP 200 and at least one posting in AGENT_LOCATIONS):", "",
             "| company | ATS | slug | board name | postings | India-eligible | sample |",
             "|---|---|---|---|---|---|---|"]
    kept = [(r, k) for r in results for k in r.kept]
    for r, k in kept:
        sample = f"{k.sample['title']} ({k.sample['location']})" if k.sample else ""
        lines.append(f"| {r.company} | {k.ats} | {k.slug} | {k.board_name or '-'} | "
                     f"{k.total} | {k.eligible} | {sample} |")
    if not kept:
        lines.append("| (none) | | | | | | |")
    dropped = [r for r in results if not r.kept]
    lines += ["", "Dropped:", ""]
    lines += [f"- {r.company}: {r.reason}" for r in dropped] or ["- (none)"]
    if kept:
        lines += ["", "To collect them, add to backend/job_scraper/ats_collector.py:"]
        for source in ATS_ORDER:
            slugs = [k.slug for _, k in kept if k.ats == source]
            if slugs:
                lines.append(f"  {source.upper()}_COMPANIES: {', '.join(map(repr, slugs))}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.job_scraper.probe_boards",
        description="Probe the public Greenhouse, Lever and Ashby job-board APIs for "
                    "companies' boards; keep only boards with a posting in "
                    "AGENT_LOCATIONS.")
    parser.add_argument("names", nargs="+",
                        help='company names; "Name=slug1,slug2" tries exactly those slugs')
    parser.add_argument("--ats", default=",".join(ATS_ORDER),
                        help="comma-separated subset of greenhouse,lever,ashby")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_S,
                        help=f"seconds between requests (default {DEFAULT_DELAY_S})")
    parser.add_argument("--json", dest="json_out", default=None,
                        help="also write every attempt as JSON to this file")
    args = parser.parse_args(argv)
    sources = tuple(s.strip() for s in args.ats.split(",") if s.strip())
    if not sources or set(sources) - set(ATS_ORDER):
        parser.error(f"--ats must name some of {', '.join(ATS_ORDER)}")
    try:
        targets = [parse_target(a) for a in args.names]
        policy = _policy()
    except ValueError as exc:
        parser.error(str(exc))
    results = []
    for name, slugs in targets:
        print(f"probing {name}: {', '.join(slugs)}", file=sys.stderr, flush=True)
        results.append(probe_company(name, slugs, policy=policy, delay_s=args.delay,
                                     ats=sources))
    print(report(results))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump([{"company": r.company, "slugs": r.slugs, "reason": r.reason,
                        "kept": [asdict(k) for k in r.kept],
                        "attempts": [asdict(a) for a in r.attempts]} for r in results],
                      f, indent=2, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
