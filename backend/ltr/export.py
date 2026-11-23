"""
export.py -- the learning-to-rank dataset: one row per (user, job) that has
a decision or an outcome, labelled by how far it got. EXPORT ONLY: nothing
is trained here, and nothing reads this back into scoring.

    python -m backend.ltr.export --out data/ltr/outcomes.csv
    GET /v1/ltr/export                       (the same CSV)

Labels (the furthest stage the job reached; a later step never lowers it):

  0  declined (you declined the agent's application), expired (nobody
     decided within AGENT_APPROVAL_TTL_DAYS) or dismissed (you passed on it)
  1  approved (you approved the agent's application packet)
  2  applied (you logged applying), or rejected (an employer's answer to an
     application, so you had applied)
  3  interview or offer

Impressions, clicks, saves and the agent's own 'drafted' events are not
decisions: a job with only those has no row.

Columns (COLUMNS, in this order):

  user_id, job_id, application_id   ids (application_id empty for a job
                                    the agent never drafted for)
  company, title, location          the job (the matched posting's location)
  label, label_name                 above; label_name is the event that set
                                    it (declined, expired, dismiss,
                                    approved, applied, rejected, interview,
                                    offer)
  match_score, skill_overlap,       the score features AT DECISION TIME: the
  semantic, role, seniority         snapshot on the first labelled event,
                                    else the application's job_scores row
  cache_key                         the scoring regime of the application's
                                    score ("<FORMULA_VERSION>:<fingerprint>")
  memory_boost, memory_effects      triage's memory effects (agent/memory.py):
                                    the boost and the memories, as JSON
  events                            the labelled events in order ("approved>applied")
  first_event_at, label_at,         timestamps (UTC, ISO 8601): the first
  last_event_at                     labelled event, the one that set the
                                    label, the latest labelled one

Training needs at least MIN_LABELLED_OUTCOMES (50) rows.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import bindparam, text

from . import MIN_LABELLED_OUTCOMES

LABELS = {"declined": 0, "expired": 0, "dismiss": 0, "approved": 1, "applied": 2,
          "rejected": 2, "interview": 3, "offer": 3}
FEATURES = ("skill_overlap", "semantic", "role", "seniority")
COLUMNS = ("user_id", "job_id", "application_id", "company", "title", "location",
           "label", "label_name", "match_score", *FEATURES, "cache_key",
           "memory_boost", "memory_effects", "events", "first_event_at", "label_at",
           "last_event_at")
# Event types that can carry a label ('declined' covers expired too).
_LABELLED_EVENTS = ("declined", "dismiss", "approved", "applied", "rejected",
                    "interview", "offer")

_EVENTS = text("""
    SELECT e.user_id, e.job_id, e.event_type, e.source, e.match_score,
           e.score_breakdown, e.created_at, e.id,
           j.company, j.title, j.location AS job_location
    FROM events e JOIN canonical_jobs j ON j.id = e.job_id
    WHERE e.event_type IN :types
    ORDER BY e.user_id, e.job_id, e.created_at, e.id
""").bindparams(bindparam("types", expanding=True))

_APPLICATIONS = text("""
    SELECT a.id, a.user_id, a.job_id, a.cache_key, a.match_score, a.memory_effects,
           COALESCE(a.posting->>'location', j.location) AS location,
           s.score_breakdown
    FROM applications a JOIN canonical_jobs j ON j.id = a.job_id
    LEFT JOIN job_scores s ON s.job_id = a.job_id AND s.profile_hash = a.profile_hash
                          AND s.cache_key = a.cache_key
""")


def _iso(value) -> str:
    return value.isoformat() if value is not None else ""


def _num(value) -> Any:
    return "" if value is None else float(value)


def build_rows(events: Iterable[dict], applications: Iterable[dict]) -> list[dict]:
    """The labelled rows from event and application dicts (pure: the
    database reads are in export_rows)."""
    from ..database.tracker import effective_type
    apps = {(a["user_id"], a["job_id"]): a for a in applications}
    groups: dict[tuple[int, int], list[dict]] = {}
    for e in events:
        groups.setdefault((e["user_id"], e["job_id"]), []).append(e)
    rows = []
    for (user_id, job_id), evs in sorted(groups.items()):
        typed = [(effective_type(e["event_type"], e["source"]), e) for e in evs]
        typed = [(t, e) for t, e in typed if t in LABELS]
        if not typed:
            continue
        label_name, label_event = typed[0]
        for t, e in typed[1:]:
            if LABELS[t] > LABELS[label_name]:   # furthest stage; first to reach it
                label_name, label_event = t, e
        app = apps.get((user_id, job_id)) or {}
        snapshot = next((e for _, e in typed if e["score_breakdown"]), None)
        breakdown = (snapshot or {}).get("score_breakdown") or app.get("score_breakdown") or {}
        match_score = (snapshot or {}).get("match_score")
        if match_score is None:
            match_score = next((e["match_score"] for _, e in typed
                                if e["match_score"] is not None), app.get("match_score"))
        effects = app.get("memory_effects")
        first = evs[0]
        rows.append({
            "user_id": user_id, "job_id": job_id, "application_id": app.get("id", ""),
            "company": first["company"], "title": first["title"],
            "location": app.get("location") or first["job_location"] or "",
            "label": LABELS[label_name], "label_name": label_name,
            "match_score": _num(match_score),
            **{f: _num(breakdown.get(f)) for f in FEATURES},
            "cache_key": app.get("cache_key", ""),
            "memory_boost": _num((effects or {}).get("boost")) if effects else 0.0,
            "memory_effects": json.dumps(effects, sort_keys=True) if effects else "",
            "events": ">".join(t for t, _ in typed),
            "first_event_at": _iso(typed[0][1]["created_at"]),
            "label_at": _iso(label_event["created_at"]),
            "last_event_at": _iso(typed[-1][1]["created_at"]),
        })
    return rows


def export_rows() -> list[dict]:
    """Every labelled row in the database."""
    from ..database.db import get_engine, init_db
    init_db()
    with get_engine().connect() as conn:
        events = [dict(r) for r in conn.execute(
            _EVENTS, {"types": list(_LABELLED_EVENTS)}).mappings()]
        applications = [dict(r) for r in conn.execute(_APPLICATIONS).mappings()]
    return build_rows(events, applications)


def to_csv(rows: list[dict]) -> str:
    """The CSV text: the header, always, then one line per row."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.ltr.export",
        description="Export the learning-to-rank dataset (one labelled row per job "
                    "with a decision or an outcome) as CSV.")
    parser.add_argument("--out", default="data/ltr/outcomes.csv",
                        help="where to write the CSV (default data/ltr/outcomes.csv)")
    args = parser.parse_args(argv)
    from ..database.corpus import db_configured, probe_database
    problem = (None if db_configured() else "no database configured (set DATABASE_URL "
               "or DB_HOST)")
    if problem is None:
        exc = probe_database()
        if exc is not None:
            problem = f"database unreachable ({exc.__class__.__name__})"
    if problem:
        print(f"ltr export: {problem}; the outcomes live in Postgres.", file=sys.stderr)
        return 2
    rows = export_rows()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_csv(rows), encoding="utf-8", newline="")
    by_label = {lbl: sum(1 for r in rows if r["label"] == lbl) for lbl in (0, 1, 2, 3)}
    print(f"Wrote {len(rows)} labelled outcome(s) to {out} (by label: {by_label}). "
          f"Training needs at least {MIN_LABELLED_OUTCOMES}: "
          f"{len(rows)}/{MIN_LABELLED_OUTCOMES}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
