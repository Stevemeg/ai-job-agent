"""
gmail_auth.py -- OAuth for the Gmail drafts integration (gmail.py).

Enabled only when GMAIL_CREDENTIALS_FILE is set: the path of a Google
Cloud "Desktop app" OAuth client (the JSON Google lets you download, with
an "installed" section). Authorize once, on a machine with a browser:

    GMAIL_CREDENTIALS_FILE=secrets/client.json python -m backend.integrations.gmail_auth

That runs the installed-app flow (a loopback redirect to 127.0.0.1 on a
random port, PKCE S256, a random state) for ONE scope, gmail.compose, and
writes the token to GMAIL_TOKEN_FILE (default data/secrets/gmail_token.json,
gitignored, mode 0600 on POSIX). The worker reads that file and refreshes
the access token with the refresh token when it is about to expire.

Note: gmail.compose would also allow sending. The scope does not stop it;
gmail.py does, by having no code that can (ADR-001 guardrail 2).

Plain HTTPS with requests (already a dependency): no Google client
library is needed for one scope, two token calls and two Gmail endpoints.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

from ..config import DATA_DIR

SCOPE = "https://www.googleapis.com/auth/gmail.compose"
DEFAULT_TOKEN_FILE = DATA_DIR / "secrets" / "gmail_token.json"
AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REFRESH_MARGIN_S = 120
TIMEOUT_S = 30


class GmailNotConfigured(RuntimeError):
    """GMAIL_CREDENTIALS_FILE is not set."""


class GmailAuthError(RuntimeError):
    """The client file or token is missing or invalid, or Google refused
    the token; the message says how to fix it."""


def configured() -> bool:
    """Gmail drafts are on only when GMAIL_CREDENTIALS_FILE is set."""
    return bool((os.environ.get("GMAIL_CREDENTIALS_FILE") or "").strip())


def credentials_file() -> Path:
    raw = (os.environ.get("GMAIL_CREDENTIALS_FILE") or "").strip()
    if not raw:
        raise GmailNotConfigured("GMAIL_CREDENTIALS_FILE is not set")
    return Path(raw)


def token_file() -> Path:
    raw = (os.environ.get("GMAIL_TOKEN_FILE") or "").strip()
    return Path(raw) if raw else DEFAULT_TOKEN_FILE


def _client() -> dict:
    path = credentials_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GmailAuthError(f"can't read the OAuth client file {path} ({exc})") from None
    client = data.get("installed") or data.get("web") or {}
    if not client.get("client_id") or not client.get("client_secret"):
        raise GmailAuthError(f"{path} is not a Google OAuth client file "
                             "(no installed.client_id / client_secret)")
    return {"client_id": client["client_id"], "client_secret": client["client_secret"],
            "auth_uri": client.get("auth_uri") or AUTH_URI,
            "token_uri": client.get("token_uri") or TOKEN_URI}


def _write_token(token: dict) -> None:
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(token, indent=2), encoding="utf-8")
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_token() -> dict:
    path = token_file()
    try:
        token = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise GmailAuthError(
            f"no Gmail token at {path}: authorize once with "
            "`python -m backend.integrations.gmail_auth`") from None
    except (OSError, ValueError) as exc:
        raise GmailAuthError(f"can't read the Gmail token {path} ({exc})") from None
    if SCOPE not in (token.get("scope") or "").split():
        raise GmailAuthError(f"the token at {path} was not granted {SCOPE}; "
                             "authorize again")
    return token


def _token_request(http: Any, token_uri: str, form: dict) -> dict:
    resp = http.post(token_uri, data=form, timeout=TIMEOUT_S)
    if resp.status_code != 200:
        raise GmailAuthError(f"Google's token endpoint refused the request "
                             f"(HTTP {resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _with_expiry(answer: dict, previous: dict | None = None) -> dict:
    token = dict(previous or {})
    token.update({k: v for k, v in answer.items() if k != "expires_in"})
    token["expires_at"] = time.time() + float(answer.get("expires_in") or 3600)
    return token


class _BearerSession:
    """The HTTP layer gmail.py talks through: a requests.Session that adds
    the bearer token and refreshes it shortly before it expires."""

    def __init__(self, http: Any = None):
        import requests
        self._http = http or requests.Session()
        self._token = _read_token()
        self._lock = threading.Lock()

    def _bearer(self) -> str:
        with self._lock:
            if float(self._token.get("expires_at") or 0) - REFRESH_MARGIN_S < time.time():
                refresh = self._token.get("refresh_token")
                if not refresh:
                    raise GmailAuthError("the Gmail token has expired and has no "
                                         "refresh token; authorize again")
                client = _client()
                answer = _token_request(self._http, client["token_uri"], {
                    "client_id": client["client_id"],
                    "client_secret": client["client_secret"],
                    "refresh_token": refresh, "grant_type": "refresh_token"})
                self._token = _with_expiry(answer, self._token)
                _write_token(self._token)
            return self._token["access_token"]

    def request(self, method: str, url: str, **kwargs):
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {self._bearer()}"}
        return self._http.request(method, url, headers=headers,
                                  timeout=kwargs.pop("timeout", TIMEOUT_S), **kwargs)


def authorized_session() -> _BearerSession:
    """The session gmail.py uses by default. GmailNotConfigured without
    GMAIL_CREDENTIALS_FILE; GmailAuthError without a usable token."""
    credentials_file()
    return _BearerSession()


# --------------------------------------------------------------------------
# the one-time installed-app flow
# --------------------------------------------------------------------------
def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize(open_browser: bool = True) -> Path:
    """Run the installed-app flow and write the token file; returns its path."""
    import requests
    import webbrowser

    client = _client()
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                                   # noqa: N802
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authorized. You can close this tab.")

        def log_message(self, *args):                       # keep the console quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
    url = client["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": client["client_id"], "redirect_uri": redirect_uri,
        "response_type": "code", "scope": SCOPE, "access_type": "offline",
        "prompt": "consent", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256"})
    print("Open this URL to let the job agent create Gmail DRAFTS (it never sends):")
    print(url, flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        while not result:
            server.handle_request()
    finally:
        server.server_close()
    if result.get("error"):
        raise GmailAuthError(f"authorization was refused: {result['error']}")
    if not secrets.compare_digest(result.get("state", ""), state):
        raise GmailAuthError("the OAuth state did not match; try again")
    answer = _token_request(requests.Session(), client["token_uri"], {
        "code": result["code"], "client_id": client["client_id"],
        "client_secret": client["client_secret"], "redirect_uri": redirect_uri,
        "grant_type": "authorization_code", "code_verifier": verifier})
    token = _with_expiry(answer)
    if SCOPE not in (token.get("scope") or "").split():
        raise GmailAuthError(f"Google did not grant {SCOPE}")
    _write_token(token)
    return token_file()


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        prog="python -m backend.integrations.gmail_auth",
        description="Authorize the job agent to create Gmail drafts (gmail.compose).")
    parser.add_argument("--no-browser", action="store_true",
                        help="print the URL instead of opening a browser")
    args = parser.parse_args(argv)
    try:
        path = authorize(open_browser=not args.no_browser)
    except (GmailNotConfigured, GmailAuthError) as exc:
        print(f"gmail_auth: {exc}")
        return 1
    print(f"Token written to {path}. The worker can now create Gmail drafts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
