"""
packet.py -- what executing an approved application produces (Phase 5).
No LLM, no network: deterministic functions of what was approved.

- build_packet(): the application packet, written to
  data/applications/<id>/packet.json. It is everything the human needs to
  apply: the approved documents (paths + sha256), the apply link, a job
  summary, why it matched and the gaps, and the validator's verdict. The
  agent never submits; the human opens apply_link and does.
- contact_email(): an application contact in the JD, or None. Addresses
  that are plainly not for applications (no-reply, privacy, accommodation,
  legal, security, support, ...) are skipped; hiring addresses (careers@,
  jobs@, recruiting@, talent@, hr@, ...) are preferred.
- draft_message(): the ONE email draft for it -- the approved cover-letter
  paragraphs (validated, and re-validated after any edit) inside a fixed
  template, with both .docx documents attached and the deterministic
  Message-ID "<aja-<application_id>-<version>@ai-job-agent>". Only ever
  saved as a Gmail draft (integrations/gmail.py cannot send).
"""
from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

DOCX_MIME = ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document")

_EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# Local-part words that mark an address as not-for-applications.
_NOT_FOR_APPLYING = ("noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
                     "privacy", "accommodat", "accessib", "legal", "security", "abuse",
                     "support", "help", "unsubscribe", "dpo", "gdpr", "compliance",
                     "ethics", "press", "media", "invoice", "billing", "sales",
                     "fraud", "notifications")
_HIRING = ("career", "job", "recruit", "talent", "hiring", "hr", "apply",
           "application", "resume", "cv", "people", "join")


def message_id_for(application_id: int, version: int) -> str:
    return f"<aja-{application_id}-{version}@ai-job-agent>"


def contact_email(text: str | None) -> str | None:
    """The JD's application contact address, or None."""
    candidates = []
    for m in _EMAIL.finditer(text or ""):
        address = m.group(0).rstrip(".")
        local = address.split("@", 1)[0].lower()
        if any(word in local for word in _NOT_FOR_APPLYING):
            continue
        if address.lower() not in (c.lower() for c in candidates):
            candidates.append(address)
    for address in candidates:
        words = re.split(r"[._+-]", address.split("@", 1)[0].lower())
        if any(w.startswith(h) for w in words for h in _HIRING):
            return address
    return candidates[0] if candidates else None


def build_packet(*, app: dict, approval: dict, artifacts: dict[str, dict],
                 explanation: dict | None, jd_requirements: Any,
                 contact: str | None) -> dict:
    """The packet dict (JSON-serializable, deterministic for one approval)."""
    jd = jd_requirements if isinstance(jd_requirements, dict) else {}
    explanation = explanation or {}
    reports = {k: a.get("report") for k, a in artifacts.items()}
    return {
        "application_id": app["id"],
        "version": approval["application_version"],
        "approval_id": approval["id"],
        "approved_at": approval["decided_at"],
        "job": {
            "job_id": app["job_id"], "title": app["title"], "company": app["company"],
            "location": app.get("location"), "apply_link": app.get("apply_link"),
            "match_score": app.get("match_score"),
            "summary": jd.get("role_summary") or "",
            "must_have_skills": jd.get("must_have_skills") or [],
        },
        "why_match": {"strong_matches": explanation.get("strong_matches") or [],
                      "likely_matches": explanation.get("likely_matches") or []},
        "gaps": explanation.get("missing_skills") or [],
        "documents": {kind: {"path": a["path"], "sha256": a["sha256"]}
                      for kind, a in sorted(artifacts.items())
                      if kind in ("resume_docx", "cover_letter_docx", "cover_letter_txt")},
        "validator": {"needs_review": bool(app.get("needs_review")),
                      "resume": reports.get("resume_docx"),
                      "cover_letter": reports.get("cover_letter_docx")},
        "contact_email": contact,
        "next_step": ("Open apply_link and submit the resume and cover letter "
                      "yourself. Nothing has been sent or submitted."),
    }


def email_body(paragraphs: list[str], profile: dict) -> str:
    lines = ["Dear Hiring Team,", ""]
    for p in paragraphs:
        lines += [p, ""]
    lines += ["My resume and cover letter are attached.", "", "Sincerely,",
              profile.get("name", "")]
    lines += [v for v in (profile.get("email"), profile.get("phone")) if v]
    return "\n".join(lines) + "\n"


def draft_message(*, app: dict, version: int, profile: dict, paragraphs: list[str],
                  to: str, attachments: list[Path]) -> EmailMessage:
    """The email draft for an approved application (never sent)."""
    msg = EmailMessage()
    msg["To"] = to
    if profile.get("email"):
        msg["From"] = formataddr((profile.get("name") or "", profile["email"]))
    msg["Subject"] = f"Application: {app['title']} -- {profile.get('name', '')}".strip(" -")
    msg["Message-ID"] = message_id_for(app["id"], version)
    msg.set_content(email_body(paragraphs, profile))
    for path in attachments:
        msg.add_attachment(Path(path).read_bytes(), maintype=DOCX_MIME[0],
                           subtype=DOCX_MIME[1], filename=Path(path).name)
    return msg
