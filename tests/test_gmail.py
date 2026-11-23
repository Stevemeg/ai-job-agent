"""The Gmail integration can create drafts and find them -- and nothing
else (ADR-001 guardrail 2). The HTTP layer is a fake that fails the test on
any URL containing "/send"; no network."""
import base64
import inspect
import json

import pytest

from backend.agent import packet
from backend.integrations import gmail, gmail_auth
from _fake_gmail import DRAFTS, FakeGmailSession


@pytest.fixture
def fake(tmp_path):
    return FakeGmailSession(tmp_path / "mailbox.json",
                            on_send=lambda url: pytest.fail(f"send called: {url}"))


def _message(app_id=7, version=2, tmp_path=None):
    doc = tmp_path / "resume.docx"
    doc.write_bytes(b"PK\x03\x04 resume")
    letter = tmp_path / "cover_letter.docx"
    letter.write_bytes(b"PK\x03\x04 letter")
    return packet.draft_message(
        app={"id": app_id, "title": "AI Engineer", "company": "TestCo"},
        version=version, profile={"name": "Test Candidate", "email": "t@example.com",
                                  "phone": "+91 1"},
        paragraphs=["First paragraph.", "Second paragraph."],
        to="careers@testco.example", attachments=[doc, letter])


def test_the_wrapper_has_no_send_capability():
    assert set(gmail.__all__) == {"create_draft", "find_draft_by_message_id"}
    assert [n for n in dir(gmail) if "send" in n.lower()] == []
    own = {name for name, obj in vars(gmail).items()
           if not name.startswith("_") and callable(obj)
           and getattr(obj, "__module__", None) == gmail.__name__}
    assert own == {"create_draft", "find_draft_by_message_id", "GmailApiError"}
    for name in ("create_draft", "find_draft_by_message_id"):
        assert "send" not in inspect.getsource(getattr(gmail, name)).lower()
    # The one HTTP entry point refuses send URLs and anything outside drafts,
    # before the HTTP layer is reached.
    calls = []

    class Recorder:
        def request(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("the HTTP layer must not be reached")
    for url in (DRAFTS + "/send", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                "https://gmail.googleapis.com/gmail/v1/users/me/messages"):
        with pytest.raises(RuntimeError, match="only talks to the drafts API"):
            gmail._request(Recorder(), "POST", url)
    assert calls == []


def test_the_fake_http_layer_fails_on_send(fake):
    with pytest.raises(pytest.fail.Exception):
        fake.request("POST", DRAFTS + "/send", json={})


def test_create_draft_posts_one_draft_with_both_documents(fake, tmp_path):
    msg = _message(tmp_path=tmp_path)
    draft_id = gmail.create_draft(msg, session=fake)
    [draft] = fake.drafts()
    assert draft_id == draft["id"]
    assert draft["message_id"] == "<aja-7-2@ai-job-agent>"
    assert draft["to"] == "careers@testco.example"
    assert [a["filename"] for a in draft["attachments"]] == ["resume.docx",
                                                             "cover_letter.docx"]
    assert {a["content_type"] for a in draft["attachments"]} == {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    assert "Second paragraph." in draft["body"] and "attached" in draft["body"]
    assert dict(draft["headers"])["X-AJA-Draft-Key"] == "aja-7-2@ai-job-agent"
    assert [(m, u) for m, u, _ in fake.requests()] == [("POST", DRAFTS)]
    assert fake.send_attempts() == []


def test_a_draft_needs_its_message_id(fake, tmp_path):
    msg = _message(tmp_path=tmp_path)
    del msg["Message-ID"]
    with pytest.raises(ValueError):
        gmail.create_draft(msg, session=fake)
    assert fake.drafts() == []


def test_find_by_message_id_uses_search_first(fake, tmp_path):
    draft_id = gmail.create_draft(_message(tmp_path=tmp_path), session=fake)
    assert gmail.find_draft_by_message_id("<aja-7-2@ai-job-agent>", session=fake) == draft_id
    assert gmail.find_draft_by_message_id("<aja-7-3@ai-job-agent>", session=fake) is None


def test_find_by_message_id_scans_headers_when_search_lags(tmp_path):
    fake = FakeGmailSession(tmp_path / "mailbox.json", search=False,
                            on_send=lambda url: pytest.fail(url))
    gmail.create_draft(_message(app_id=1, version=1, tmp_path=tmp_path), session=fake)
    wanted = gmail.create_draft(_message(app_id=7, version=2, tmp_path=tmp_path),
                                session=fake)
    assert gmail.find_draft_by_message_id("<aja-7-2@ai-job-agent>", session=fake) == wanted
    assert gmail.find_draft_by_message_id("<aja-9-9@ai-job-agent>", session=fake) is None


def test_an_api_error_is_raised_not_swallowed(tmp_path):
    class Refuses:
        def request(self, *args, **kwargs):
            from _fake_gmail import FakeResponse
            return FakeResponse(403, {"error": "insufficient scope"})
    with pytest.raises(gmail.GmailApiError) as exc:
        gmail.create_draft(_message(tmp_path=tmp_path), session=Refuses())
    assert exc.value.status == 403


# ---- OAuth / configuration (no network: the token endpoint is faked) ---------

def test_gmail_is_enabled_only_by_its_credentials_file(monkeypatch):
    monkeypatch.delenv("GMAIL_CREDENTIALS_FILE", raising=False)
    assert gmail_auth.configured() is False
    with pytest.raises(gmail_auth.GmailNotConfigured):
        gmail_auth.authorized_session()
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", "client.json")
    assert gmail_auth.configured() is True


def test_the_token_lives_under_data_secrets_and_is_gitignored():
    from backend.config import DATA_DIR
    assert gmail_auth.DEFAULT_TOKEN_FILE == DATA_DIR / "secrets" / "gmail_token.json"
    ignored = (gmail_auth.DATA_DIR.parent / ".gitignore").read_text(encoding="utf-8")
    assert "data/secrets/" in ignored.splitlines()
    assert gmail_auth.SCOPE == "https://www.googleapis.com/auth/gmail.compose"


def test_the_session_refreshes_an_expiring_token(tmp_path, monkeypatch):
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "cid", "client_secret": "cs"}}))
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"access_token": "old", "refresh_token": "r1",
                                 "scope": gmail_auth.SCOPE, "expires_at": 0}))
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(client))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(token))
    seen = []

    class HTTP:
        def post(self, url, data=None, timeout=None):
            from _fake_gmail import FakeResponse
            seen.append(("POST", url, data["grant_type"]))
            return FakeResponse(200, {"access_token": "new", "expires_in": 3600,
                                      "scope": gmail_auth.SCOPE})

        def request(self, method, url, headers=None, timeout=None, **kwargs):
            from _fake_gmail import FakeResponse
            seen.append((method, url, headers["Authorization"]))
            return FakeResponse(200, {"id": "d1"})
    session = gmail_auth._BearerSession(http=HTTP())
    session.request("POST", DRAFTS, json={})
    assert seen == [("POST", gmail_auth.TOKEN_URI, "refresh_token"),
                    ("POST", DRAFTS, "Bearer new")]
    stored = json.loads(token.read_text())
    assert stored["access_token"] == "new" and stored["refresh_token"] == "r1"


def test_a_missing_token_says_how_to_authorize(tmp_path, monkeypatch):
    monkeypatch.setenv("GMAIL_CREDENTIALS_FILE", str(tmp_path / "client.json"))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(tmp_path / "none.json"))
    with pytest.raises(gmail_auth.GmailAuthError, match="gmail_auth"):
        gmail_auth.authorized_session()


def test_pkce_challenge_is_s256_of_the_verifier():
    import hashlib
    verifier, challenge = gmail_auth._pkce()
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected and 43 <= len(verifier) <= 128
