"""
gmail.py -- Gmail DRAFTS, nothing else (ADR-001 guardrail 2).

The whole surface is two functions:

  create_draft(message)               POST users/me/drafts -> the draft id
  find_draft_by_message_id(msg_id)    the id of the draft whose Message-ID
                                      header is msg_id, or None

There is no function, method or code path that sends mail. Every request
goes through _request(), which refuses any URL outside users/me/drafts
and any URL containing "/send", so even a future edit can't reach
messages.send or drafts.send through this module. The OAuth scope
(gmail.compose) would allow sending -- this module is what doesn't.

The HTTP layer is a session with request(method, url, **kw)
(gmail_auth.authorized_session() by default); tests pass a fake one.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

__all__ = ["create_draft", "find_draft_by_message_id"]

_DRAFTS = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
# Drafts scanned by header when Gmail's search hasn't indexed a new draft
# yet (it can lag right after create -- exactly when a retry looks).
_SCAN_LIMIT = 200
# Also written on every draft: Gmail search can't query X- headers, but
# the scan compares it when a Message-ID was rewritten.
_KEY_HEADER = "X-AJA-Draft-Key"


class GmailApiError(RuntimeError):
    """Gmail answered with an HTTP error."""

    def __init__(self, status: int, body: str):
        super().__init__(f"Gmail API error (HTTP {status}): {body[:300]}")
        self.status = status


def _session(session: Any) -> Any:
    if session is not None:
        return session
    from .gmail_auth import authorized_session
    return authorized_session()


def _request(session: Any, method: str, url: str, **kwargs) -> dict:
    if not url.startswith(_DRAFTS) or "/send" in url:
        raise RuntimeError(f"gmail.py only talks to the drafts API, not {url}")
    resp = session.request(method, url, **kwargs)
    if resp.status_code >= 400:
        raise GmailApiError(resp.status_code, resp.text)
    return resp.json() if resp.content else {}


def create_draft(message: EmailMessage, *, session: Any = None) -> str:
    """Create ONE draft from `message` (which must carry its Message-ID)
    and return the draft id. It stays a draft."""
    if not message.get("Message-ID"):
        raise ValueError("a draft needs a Message-ID (exactly-once lookup)")
    if _KEY_HEADER not in message:
        message[_KEY_HEADER] = message["Message-ID"].strip("<>")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    answer = _request(_session(session), "POST", _DRAFTS, json={"message": {"raw": raw}})
    return answer["id"]


def _headers(draft: dict) -> dict:
    headers = ((draft.get("message") or {}).get("payload") or {}).get("headers") or []
    return {h.get("name", "").lower(): h.get("value", "") for h in headers}


def find_draft_by_message_id(message_id: str, *, session: Any = None) -> str | None:
    """The id of the draft created with this Message-ID, or None. Gmail's
    search first (rfc822msgid:), then the newest drafts' headers, since a
    draft created moments ago may not be searchable yet."""
    s = _session(session)
    bare = message_id.strip().strip("<>")
    found = _request(s, "GET", _DRAFTS, params={"q": f"rfc822msgid:{bare}",
                                                 "maxResults": 10})
    for draft in found.get("drafts") or []:
        return draft["id"]
    seen, page = 0, None
    while seen < _SCAN_LIMIT:
        params = {"maxResults": 100}
        if page:
            params["pageToken"] = page
        listing = _request(s, "GET", _DRAFTS, params=params)
        for draft in listing.get("drafts") or []:
            seen += 1
            full = _request(s, "GET", f"{_DRAFTS}/{draft['id']}",
                            params={"format": "metadata"})
            headers = _headers(full)
            if (headers.get("message-id", "").strip().strip("<>") == bare
                    or headers.get(_KEY_HEADER.lower(), "").strip() == bare):
                return draft["id"]
        page = listing.get("nextPageToken")
        if not page:
            break
    return None
