"""
auth.py -- the API's bearer token for mutating endpoints.

With API_TOKEN set, every endpoint that changes state (POST/PUT/PATCH/
DELETE) needs "Authorization: Bearer <API_TOKEN>"; a missing or wrong
token is 401. Without API_TOKEN nothing is checked (local development, as
before) -- so the API must never be exposed beyond localhost or a private
network without it. Reads stay unauthenticated either way, and they
include personal documents (resumes, cover letters): keep the API off the
public internet regardless (docs/DEPLOYMENT.md).

The token is compared in constant time and never logged.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

from fastapi import Header, HTTPException


def api_token() -> str:
    return (os.environ.get("API_TOKEN") or "").strip()


def require_token(authorization: Optional[str] = Header(None)) -> None:
    """FastAPI dependency for every mutating route (read at request time,
    so a changed API_TOKEN applies without a restart)."""
    expected = api_token()
    if not expected:
        return
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
            supplied.strip().encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(401, "Missing or invalid bearer token (API_TOKEN is set).",
                            headers={"WWW-Authenticate": "Bearer"})
