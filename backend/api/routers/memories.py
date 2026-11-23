"""Agent memory endpoints (Phase 6): what the agent remembers about your
preferences, and how it got there. Memories only steer TRIAGE: an active
*_avoid memory keeps matching jobs out, an active *_prefer memory moves
matching jobs up the order (never past the AGENT_MIN_SCORE gate, never
into a score). See backend/agent/memory.py.

- GET    /v1/memories[?kind=&active=&provenance=]  every memory, with its
         provenance (stated | learned) and evidence
- POST   /v1/memories       state one (201). Stating a learned one makes it
         yours (provenance stated); one you already stated is a 409
- PATCH  /v1/memories/{id}  edit value / weight / active. Editing a learned
         memory's value or weight makes it stated; deactivating keeps it
         learned but frozen (never re-learned)
- DELETE /v1/memories/{id}  remove it (204). A learned one can be learned
         again from the same decisions: deactivate it to stop that

Mutating endpoints need "Authorization: Bearer <API_TOKEN>" when
API_TOKEN is set. Every endpoint needs the database (503 without it).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..auth import require_token
from ..deps import load_profile
from ..schemas import MemoryCreate, MemoryRecord, MemoryUpdate

router = APIRouter(prefix="/memories", tags=["memory"])


def _user_id() -> int:
    # DB imports deferred: the API boots (and other routes work) without
    # Postgres; only these endpoints need it.
    try:
        from ...database.db import init_db
        from ...database.tracker import get_or_create_user
        init_db()
    except Exception as exc:                                # noqa: BLE001
        raise HTTPException(503, f"Memory database unavailable ({exc.__class__.__name__}). "
                                 "Set DATABASE_URL or DB_HOST.")
    profile = load_profile()
    return get_or_create_user(profile.get("email") or "local@user", profile.get("name", ""))


def _fail(exc) -> HTTPException:
    return HTTPException(exc.status_code, str(exc))


@router.get("", response_model=list[MemoryRecord])
def list_memories(kind: Optional[str] = Query(None),
                  active: Optional[bool] = Query(None),
                  provenance: Optional[str] = Query(None, pattern="^(stated|learned)$")):
    from ...agent import memory
    from ...database.db import MEMORY_KINDS
    if kind is not None and kind not in MEMORY_KINDS:
        raise HTTPException(422, f"kind must be one of {', '.join(MEMORY_KINDS)}")
    return memory.list_memories(_user_id(), kind=kind, active=active, provenance=provenance)


@router.post("", response_model=MemoryRecord, status_code=201,
             dependencies=[Depends(require_token)])
def create_memory(body: MemoryCreate):
    from ...agent import memory
    user_id = _user_id()
    try:
        return memory.create_stated(user_id, kind=body.kind, value=body.value,
                                    weight=body.weight, via="api")
    except memory.MemoryRequestError as exc:
        raise _fail(exc)


@router.patch("/{memory_id}", response_model=MemoryRecord,
              dependencies=[Depends(require_token)])
def update_memory(memory_id: int, body: MemoryUpdate):
    from ...agent import memory
    user_id = _user_id()
    try:
        return memory.update(user_id, memory_id, value=body.value, weight=body.weight,
                             active=body.active)
    except memory.MemoryRequestError as exc:
        raise _fail(exc)


@router.delete("/{memory_id}", status_code=204, dependencies=[Depends(require_token)])
def delete_memory(memory_id: int):
    from ...agent import memory
    if not memory.delete(_user_id(), memory_id):
        raise HTTPException(404, f"No memory {memory_id}.")
    return Response(status_code=204)
