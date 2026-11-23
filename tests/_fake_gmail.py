"""A fake Gmail HTTP layer for the drafts integration (not collected by
pytest). It stands in for the session backend/integrations/gmail.py talks
through: request(method, url, params=..., json=...). State lives in a JSON
file so several worker processes (the kill test) share one "mailbox".

- POST   .../users/me/drafts              stores the draft (raw MIME parsed)
- GET    .../users/me/drafts?q=rfc822msgid:<id>   search (search=False: the
         index hasn't caught up, so it finds nothing -- as right after create)
- GET    .../users/me/drafts              list, newest first
- GET    .../users/me/drafts/<id>         format=metadata: the headers
- any URL containing "/send" -> on_send(url) (default: AssertionError), and
  the attempt is recorded in the file, so a subprocess's attempt is seen too
- anything else -> AssertionError
"""
from __future__ import annotations

import base64
import json
import os
from email import message_from_bytes, policy
from pathlib import Path
from urllib.parse import urlsplit

DRAFTS = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


def _send_attempt(url):
    raise AssertionError(f"a Gmail send endpoint was called: {url}")


class FakeGmailSession:
    def __init__(self, path: str | Path, *, search: bool = True, on_send=_send_attempt):
        self.path = Path(path)
        self.search = search
        self.on_send = on_send
        if not self.path.exists():
            self._save({"drafts": [], "send_attempts": [], "requests": []})

    # ---- the mailbox file ----------------------------------------------------
    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, self.path)

    def drafts(self) -> list[dict]:
        return self._load()["drafts"]

    def send_attempts(self) -> list[str]:
        return self._load()["send_attempts"]

    def requests(self) -> list[list]:
        return self._load()["requests"]

    # ---- the HTTP layer --------------------------------------------------------
    def request(self, method, url, params=None, json=None, headers=None, **_):
        data = self._load()
        data["requests"].append([method, url, params or {}])
        self._save(data)
        if "/send" in url:
            data["send_attempts"].append(url)
            self._save(data)
            self.on_send(url)
            return FakeResponse(400, {"error": "send is not allowed"})
        if not url.startswith(DRAFTS):
            raise AssertionError(f"unexpected Gmail URL {url}")
        rest = urlsplit(url).path.split("/drafts", 1)[1].strip("/")
        if method == "POST" and not rest:
            return self._create(data, json)
        if method == "GET" and not rest:
            return self._list(data, params or {})
        if method == "GET" and rest:
            return self._get(data, rest)
        raise AssertionError(f"unexpected Gmail call {method} {url}")

    def _create(self, data, body):
        raw = base64.urlsafe_b64decode(body["message"]["raw"])
        msg = message_from_bytes(raw, policy=policy.default)
        n = len(data["drafts"]) + 1
        draft = {"id": f"r-draft-{n}", "message_id": msg["Message-ID"],
                 "headers": [[k, str(v)] for k, v in msg.items()],
                 "to": msg["To"], "subject": msg["Subject"],
                 "attachments": [{"filename": part.get_filename(),
                                  "content_type": part.get_content_type(),
                                  "size": len(part.get_payload(decode=True) or b"")}
                                 for part in msg.iter_attachments()],
                 "body": msg.get_body(("plain",)).get_content()}
        data["drafts"].append(draft)
        self._save(data)
        return FakeResponse(200, {"id": draft["id"], "message": {"id": f"m{n}"}})

    def _list(self, data, params):
        q = params.get("q")
        drafts = list(reversed(data["drafts"]))           # newest first
        if q is not None:
            if not self.search:
                drafts = []
            else:
                want = q.split("rfc822msgid:", 1)[1].strip("<>")
                drafts = [d for d in drafts if d["message_id"].strip("<>") == want]
        return FakeResponse(200, {"drafts": [{"id": d["id"]} for d in drafts]}
                            if drafts else {})

    def _get(self, data, draft_id):
        for d in data["drafts"]:
            if d["id"] == draft_id:
                return FakeResponse(200, {"id": d["id"], "message": {"payload": {
                    "headers": [{"name": k, "value": v} for k, v in d["headers"]]}}})
        return FakeResponse(404, {"error": "not found"})
