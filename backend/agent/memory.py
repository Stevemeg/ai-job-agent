"""
memory.py -- what the agent remembers about a user's preferences (Phase 6).

TRIAGE ONLY. A memory never changes a match_score, the AGENT_MIN_SCORE
gate, job_scores or any ranking output. It changes which of the jobs
that already passed the gate the agent drafts for, and in what order:

  company_avoid    a job at this company is not triaged
  title_avoid      a job whose title contains these word(s) is not triaged
  company_prefer   a job at this company moves up the triage order
  title_prefer     a job whose title contains these word(s) moves up
  location_prefer  a job whose location contains these word(s) moves up
  note             free text for the user; no effect

Matching: a company value compares on its letters and digits only, case
ignored ("Sarvam AI" = "sarvam-ai" = the collector's slug "sarvamai").
Title and location values match as whole words, the way AGENT_LOCATIONS
and AGENT_EXCLUDE_TITLE_WORDS do.

The boost: every active *_prefer memory that matches adds its weight
(score points) to the job's triage ORDER key, capped in total at
AGENT_MEMORY_MAX_BOOST (5). Jobs are then taken best key first. The key
only orders jobs that already passed the gate, so a boost can never let a
job through it, and the application keeps the job's real match_score.
Each application records the memories that boosted it
(applications.memory_effects).

Provenance:

  stated   from the user (POST /v1/memories, the Inbox). Learning never
           changes it. Stating a memory that was learned makes it stated.
  learned  derived by learn() from the user's own approve / decline
           decisions (approvals, source 'user'; expiries and edits don't
           count), deterministically, no LLM:
             company_avoid   a company declined >= AGENT_LEARN_COMPANY_DECLINES
                             (2) times and never approved
             title_avoid     a title word declined >= AGENT_LEARN_TITLE_DECLINES
                             (3) times and never approved
             *_prefer        a company, title word or location approved
                             >= AGENT_LEARN_PREFER_APPROVALS (2) times, more
                             often than declined; weight = approvals - declines
           Its evidence lists every decision behind it (approval, application,
           job and event ids, the decline reasons). learn() recomputes all
           learned memories from scratch, so it is idempotent: a memory whose
           evidence no longer meets its rule is removed.

Learning never contradicts the user: it learns no company_avoid for a
company the user actively stated company_prefer for (and the reverse;
titles likewise).

A deactivated memory has no effect, and learning never touches it again
(never reactivated, re-weighted or removed): deactivate a learned memory
to stop it coming back. Editing a learned memory's value or weight makes
it stated. Deleting one is allowed, but the same decisions can teach it
again.

Title words for learning skip generic ones ("engineer", "software", ...)
and location words; locations learn from the AGENT_LOCATIONS entry the
application's location matched (never "India", which nearly every
eligible job has) plus "remote" for remote jobs.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from ..database.db import MEMORY_KINDS, MEMORY_PROVENANCES, get_engine, init_db
from .locations import DEFAULT_LOCATIONS, REMOTE_WORDS, _contains, words

log = logging.getLogger(__name__)

AVOID_KINDS = ("company_avoid", "title_avoid")
PREFER_KINDS = ("company_prefer", "title_prefer", "location_prefer")
MAX_VALUE_CHARS = 200
MAX_NOTE_CHARS = 2000
MAX_WEIGHT = 100.0
STATED_DEFAULT_WEIGHT = 2.0          # a stated preference counts like two approvals
# pg_advisory_xact_lock(MEMORY_LOCK_KEY, user_id): one learner per user at a time.
MEMORY_LOCK_KEY = 0x6D656D6F         # b"memo"

GENERIC_TITLE_WORDS = frozenset({
    "a", "an", "and", "at", "for", "in", "of", "on", "the", "to", "with", "i", "ii",
    "iii", "iv", "v", "engineer", "engineers", "engineering", "developer", "developers",
    "software", "new", "grad", "team", "role", "position", "job", "remote", "hybrid",
    "onsite", "full", "time", "fulltime", "part", "contract"})


class MemoryRequestError(Exception):
    """A memory request that can't be done; .status_code is the HTTP status."""
    status_code = 400


class MemoryNotFound(MemoryRequestError):
    status_code = 404


class MemoryConflict(MemoryRequestError):
    status_code = 409


class InvalidMemory(MemoryRequestError):
    status_code = 422


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------
def company_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z]", "", str(value or "").casefold())


def _needle(kind: str, value: str):
    return company_key(value) if kind.startswith("company_") else words(value)


def _matches(kind: str, needle, *, company: Any, title: Any, location: Any) -> bool:
    if kind.startswith("company_"):
        return bool(needle) and company_key(company) == needle
    if kind.startswith("title_"):
        return _contains(words(title), needle)
    if kind == "location_prefer":
        return _contains(words(location), needle)
    return False


@dataclass
class MemorySet:
    """The active memories triage applies, compiled once per run."""
    memories: list[dict] = field(default_factory=list)
    max_boost: float = 5.0

    def __post_init__(self) -> None:
        self._compiled = [(m, _needle(m["kind"], m["value"])) for m in self.memories
                          if m.get("active", True) and m["kind"] in AVOID_KINDS + PREFER_KINDS]

    def __len__(self) -> int:
        return len(self._compiled)

    @property
    def reorders(self) -> bool:
        return self.max_boost > 0 and any(m["kind"] in PREFER_KINDS and float(m["weight"]) > 0
                                          for m, _ in self._compiled)

    def avoided_by(self, **job) -> dict | None:
        """The first active *_avoid memory this job matches, or None."""
        for m, needle in self._compiled:
            if m["kind"] in AVOID_KINDS and _matches(m["kind"], needle, **job):
                return m
        return None

    def effects(self, **job) -> dict | None:
        """{"boost", "memories"} for the *_prefer memories this job
        matches, or None if none do."""
        hits = [m for m, needle in self._compiled
                if m["kind"] in PREFER_KINDS and float(m["weight"]) > 0
                and _matches(m["kind"], needle, **job)]
        if not hits:
            return None
        boost = min(self.max_boost, sum(float(m["weight"]) for m in hits))
        return {"boost": round(boost, 4),
                "memories": [{"id": m["id"], "kind": m["kind"], "value": m["value"],
                              "provenance": m["provenance"], "weight": float(m["weight"])}
                             for m in hits]}

    @classmethod
    def load(cls, user_id: int, *, max_boost: float) -> "MemorySet":
        return cls(list_memories(user_id, active=True), max_boost=max_boost)


# --------------------------------------------------------------------------
# reads and the user's own (stated) memories
# --------------------------------------------------------------------------
_COLUMNS = ("id, user_id, kind, value, provenance, evidence, weight, active, "
            "created_at, updated_at")


def _row(r) -> dict:
    out = dict(r)
    out["weight"] = float(out["weight"])
    return out


def list_memories(user_id: int, *, kind: str | None = None, active: bool | None = None,
                  provenance: str | None = None) -> list[dict]:
    sql, params = f"SELECT {_COLUMNS} FROM agent_memories WHERE user_id = :u", {"u": user_id}
    for column, value in (("kind", kind), ("active", active), ("provenance", provenance)):
        if value is not None:
            sql += f" AND {column} = :{column}"
            params[column] = value
    sql += " ORDER BY kind, lower(value), id"
    init_db()
    with get_engine().connect() as conn:
        return [_row(r) for r in conn.execute(text(sql), params).mappings()]


def get_memory(user_id: int, memory_id: int) -> dict | None:
    init_db()
    with get_engine().connect() as conn:
        row = conn.execute(text(f"SELECT {_COLUMNS} FROM agent_memories "
                                "WHERE id = :id AND user_id = :u"),
                           {"id": memory_id, "u": user_id}).mappings().first()
    return _row(row) if row is not None else None


def _clean_value(kind: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidMemory("value must be non-empty text")
    value = " ".join(value.split())
    limit = MAX_NOTE_CHARS if kind == "note" else MAX_VALUE_CHARS
    if len(value) > limit:
        raise InvalidMemory(f"value: at most {limit} characters")
    if kind != "note" and not (company_key(value) if kind.startswith("company_")
                               else words(value)):
        raise InvalidMemory("value needs a letter or digit")
    return value


def _clean_weight(weight: Any) -> float:
    try:
        w = float(weight)
    except (TypeError, ValueError):
        raise InvalidMemory("weight must be a number") from None
    if not 0 <= w <= MAX_WEIGHT:
        raise InvalidMemory(f"weight must be between 0 and {MAX_WEIGHT:g}")
    return w


def _evidence_param():
    return bindparam("evidence", type_=JSONB)


def create_stated(user_id: int, *, kind: str, value: Any, weight: Any = None,
                  via: str = "api") -> dict:
    """The user states a memory. A learned memory with the same (kind,
    value) becomes stated (kept evidence, active again). MemoryConflict if
    the user already stated it; InvalidMemory for a bad kind/value/weight."""
    if kind not in MEMORY_KINDS:
        raise InvalidMemory(f"kind must be one of {', '.join(MEMORY_KINDS)}")
    value = _clean_value(kind, value)
    w = STATED_DEFAULT_WEIGHT if weight is None else _clean_weight(weight)
    init_db()
    with get_engine().begin() as conn:
        existing = conn.execute(text(
            f"SELECT {_COLUMNS} FROM agent_memories WHERE user_id = :u AND kind = :k "
            "AND lower(value) = lower(:v) FOR UPDATE"),
            {"u": user_id, "k": kind, "v": value}).mappings().first()
        if existing is not None and existing["provenance"] == "stated":
            raise MemoryConflict(f"You already have a {kind} memory for {existing['value']!r} "
                                 f"(#{existing['id']}); edit it instead.")
        if existing is not None:
            row = conn.execute(text(
                f"UPDATE agent_memories SET provenance = 'stated', active = true, "
                f"value = :v, weight = :w, evidence = evidence || :evidence, "
                f"updated_at = now() WHERE id = :id RETURNING {_COLUMNS}"
            ).bindparams(_evidence_param()),
                {"v": value, "w": w if weight is not None else float(existing["weight"]),
                 "evidence": {"stated_via": via}, "id": existing["id"]}).mappings().one()
        else:
            row = conn.execute(text(
                f"INSERT INTO agent_memories (user_id, kind, value, provenance, evidence, "
                f"weight) VALUES (:u, :k, :v, 'stated', :evidence, :w) "
                f"RETURNING {_COLUMNS}").bindparams(_evidence_param()),
                {"u": user_id, "k": kind, "v": value, "w": w,
                 "evidence": {"stated_via": via}}).mappings().one()
    return _row(row)


def update(user_id: int, memory_id: int, *, value: Any = None, weight: Any = None,
           active: bool | None = None) -> dict:
    """Edit a memory. Changing a learned memory's value or weight makes it
    stated (the user owns it now); (de)activating keeps its provenance."""
    if value is None and weight is None and active is None:
        raise InvalidMemory("nothing to change: give value, weight and/or active")
    init_db()
    with get_engine().begin() as conn:
        m = conn.execute(text(f"SELECT {_COLUMNS} FROM agent_memories "
                              "WHERE id = :id AND user_id = :u FOR UPDATE"),
                         {"id": memory_id, "u": user_id}).mappings().first()
        if m is None:
            raise MemoryNotFound(f"No memory {memory_id}.")
        new_value = m["value"] if value is None else _clean_value(m["kind"], value)
        new_weight = float(m["weight"]) if weight is None else _clean_weight(weight)
        new_active = m["active"] if active is None else bool(active)
        edited = new_value != m["value"] or new_weight != float(m["weight"])
        provenance = "stated" if edited else m["provenance"]
        if new_value.lower() != m["value"].lower():
            clash = conn.execute(text(
                "SELECT id FROM agent_memories WHERE user_id = :u AND kind = :k "
                "AND lower(value) = lower(:v) AND id <> :id"),
                {"u": user_id, "k": m["kind"], "v": new_value, "id": memory_id}).scalar()
            if clash is not None:
                raise MemoryConflict(f"Memory #{clash} already has that {m['kind']} value.")
        row = conn.execute(text(
            f"UPDATE agent_memories SET value = :v, weight = :w, active = :a, "
            f"provenance = :p, updated_at = now() WHERE id = :id RETURNING {_COLUMNS}"),
            {"v": new_value, "w": new_weight, "a": new_active, "p": provenance,
             "id": memory_id}).mappings().one()
    return _row(row)


def delete(user_id: int, memory_id: int) -> bool:
    init_db()
    with get_engine().begin() as conn:
        return conn.execute(text("DELETE FROM agent_memories WHERE id = :id AND user_id = :u"),
                            {"id": memory_id, "u": user_id}).rowcount > 0


# --------------------------------------------------------------------------
# learning
# --------------------------------------------------------------------------
_DECISIONS = text("""
    SELECT ap.id AS approval_id, ap.decision, ap.reason, ap.decided_at,
           a.id AS application_id, a.job_id, j.company, j.title,
           COALESCE(a.posting->>'location', j.location) AS location,
           (SELECT e.id FROM events e
             WHERE e.user_id = a.user_id AND e.job_id = a.job_id AND e.source = 'user'
               AND e.event_type = CASE ap.decision WHEN 'approve' THEN 'approved'
                                                   ELSE 'declined' END
             ORDER BY e.id LIMIT 1) AS event_id
    FROM approvals ap
    JOIN applications a ON a.id = ap.application_id
    JOIN canonical_jobs j ON j.id = a.job_id
    WHERE a.user_id = :u AND ap.source = 'user' AND ap.decision IN ('approve', 'decline')
    ORDER BY ap.id
""")


def title_terms(title: Any, locations: Iterable[str] = DEFAULT_LOCATIONS) -> list[str]:
    """The words of a title that learning counts (generic and location
    words left out), each once."""
    skip = GENERIC_TITLE_WORDS | REMOTE_WORDS | {w for loc in locations for w in words(loc)}
    out = []
    for w in words(title):
        if len(w) >= 2 and not w.isdigit() and w not in skip and w not in out:
            out.append(w)
    return out


def location_terms(location: Any, locations: Iterable[str]) -> list[str]:
    """The AGENT_LOCATIONS entries a location contains (not "India"), and
    "remote" for a remote one."""
    w = words(location)
    out = [loc for loc in locations
           if loc.casefold() != "india" and _contains(w, words(loc))]
    if REMOTE_WORDS.intersection(w):
        out.append("remote")
    return out


def _evidence_item(d: dict) -> dict:
    return {"approval_id": d["approval_id"], "application_id": d["application_id"],
            "job_id": d["job_id"], "event_id": d["event_id"], "decision": d["decision"],
            "company": d["company"], "title": d["title"], "location": d["location"],
            "reason": d["reason"],
            "decided_at": d["decided_at"].isoformat() if d["decided_at"] else None}


def derive(decisions: list[dict], settings) -> dict[tuple[str, str], dict]:
    """The learned memories these decisions support: {(kind, value.lower()):
    {"kind", "value", "weight", "evidence"}}. Pure."""
    groups: dict[tuple[str, str], dict] = {}

    def add(kind_base: str, value: str, d: dict) -> None:
        g = groups.setdefault((kind_base, value.lower()), {"value": value, "approve": [],
                                                           "decline": []})
        g[d["decision"]].append(d)

    for d in decisions:
        add("company", d["company"], d)
        for term in title_terms(d["title"], settings.locations):
            add("title", term, d)
        for term in location_terms(d["location"], settings.locations):
            add("location", term, d)

    out = {}
    for (base, key), g in sorted(groups.items()):
        approved, declined = len(g["approve"]), len(g["decline"])
        evidence = {"approved": [_evidence_item(d) for d in g["approve"]],
                    "declined": [_evidence_item(d) for d in g["decline"]],
                    "decline_reasons": [d["reason"] for d in g["decline"] if d["reason"]]}
        avoid_at = {"company": settings.learn_company_declines,
                    "title": settings.learn_title_declines}.get(base)
        if avoid_at is not None and declined >= avoid_at and approved == 0:
            out[(f"{base}_avoid", key)] = {
                "kind": f"{base}_avoid", "value": g["value"], "weight": float(declined),
                "evidence": {"rule": f"declined {declined} times (>= {avoid_at}), "
                                     "never approved", **evidence}}
        if approved >= settings.learn_prefer_approvals and approved > declined:
            out[(f"{base}_prefer", key)] = {
                "kind": f"{base}_prefer", "value": g["value"],
                "weight": float(approved - declined),
                "evidence": {"rule": f"approved {approved} times (>= "
                                     f"{settings.learn_prefer_approvals}), declined "
                                     f"{declined}", **evidence}}
    return out


def _opposite(kind: str) -> str:
    base, _, polarity = kind.rpartition("_")
    return f"{base}_{'prefer' if polarity == 'avoid' else 'avoid'}"


_UPSERT_LEARNED = text("""
    INSERT INTO agent_memories (user_id, kind, value, provenance, evidence, weight)
    VALUES (:u, :k, :v, 'learned', :evidence, :w)
    ON CONFLICT (user_id, kind, lower(value)) DO UPDATE SET
        evidence = EXCLUDED.evidence, weight = EXCLUDED.weight, updated_at = now()
    WHERE agent_memories.provenance = 'learned' AND agent_memories.active
      AND (agent_memories.evidence <> EXCLUDED.evidence
           OR agent_memories.weight <> EXCLUDED.weight)
    RETURNING id, (xmax = 0) AS inserted
""").bindparams(_evidence_param())


def learn(user_id: int, settings=None) -> dict:
    """Recompute the user's learned memories from their decisions (one
    transaction, one learner per user). Stated and deactivated memories
    are never touched. Returns {"created", "updated", "removed", "learned"}."""
    from .settings import settings_from_env
    s = settings or settings_from_env()
    init_db()
    with get_engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k, :u)"),
                     {"k": MEMORY_LOCK_KEY, "u": user_id})
        decisions = [dict(r) for r in conn.execute(_DECISIONS, {"u": user_id}).mappings()]
        wanted = derive(decisions, s)
        stated = {(r.kind, r.v) for r in conn.execute(text(
            "SELECT kind, lower(value) AS v FROM agent_memories WHERE user_id = :u "
            "AND provenance = 'stated' AND active"), {"u": user_id})}
        wanted = {(kind, key): m for (kind, key), m in wanted.items()
                  if (_opposite(kind), key) not in stated}
        created = updated = 0
        for m in wanted.values():
            row = conn.execute(_UPSERT_LEARNED, {"u": user_id, "k": m["kind"], "v": m["value"],
                                                 "evidence": m["evidence"],
                                                 "w": m["weight"]}).first()
            if row is not None:
                created += bool(row.inserted)
                updated += not row.inserted
        stale = [r.id for r in conn.execute(text(
            "SELECT id, kind, lower(value) AS v FROM agent_memories "
            "WHERE user_id = :u AND provenance = 'learned' AND active"),
            {"u": user_id}) if (r.kind, r.v) not in wanted]
        if stale:
            conn.execute(text("DELETE FROM agent_memories WHERE id = ANY(:ids)"),
                         {"ids": stale})
    if created or updated or stale:
        log.info("memory: user %s learned %d new, %d updated, %d removed", user_id,
                 created, updated, len(stale))
    return {"created": created, "updated": updated, "removed": len(stale),
            "learned": len(wanted)}


def learn_safely(user_id: int, settings=None) -> dict:
    """learn(), but a failure is logged and reported, never raised: a
    learning bug must not fail a decision or a run (the next triage
    learns again)."""
    try:
        return learn(user_id, settings)
    except Exception as exc:                                # noqa: BLE001
        log.exception("memory: learning for user %s failed", user_id)
        return {"error": f"{exc.__class__.__name__}: {exc}"}


__all__ = ["MemoryRequestError", "MemoryNotFound", "MemoryConflict", "InvalidMemory",
           "MemorySet", "list_memories", "get_memory", "create_stated", "update", "delete",
           "learn", "learn_safely", "derive", "title_terms", "location_terms", "company_key",
           "MEMORY_KINDS", "MEMORY_PROVENANCES"]
