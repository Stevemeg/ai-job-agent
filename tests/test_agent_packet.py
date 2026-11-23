"""The execution helpers (backend/agent/packet.py): the contact email a JD
names, the application packet, and the email draft. Pure functions."""
import pytest

from backend.agent import packet


@pytest.mark.parametrize("jd, expected", [
    ("Apply at careers@acme.example today.", "careers@acme.example"),
    ("Questions: info@acme.example. Resumes to jobs@acme.example.", "jobs@acme.example"),
    ("Send your CV to Priya.Sharma@acme.example.", "Priya.Sharma@acme.example"),
    ("For accommodations email accommodations@acme.example; privacy: "
     "privacy@acme.example; no-reply@acme.example sends updates.", None),
    ("Need help? support@acme.example. Apply: talent.acquisition@acme.example",
     "talent.acquisition@acme.example"),
    ("chris@acme.example or hr@acme.example", "hr@acme.example"),   # 'chris' is no 'hr'
    ("No address here at all.", None),
    ("", None),
    (None, None),
])
def test_contact_email(jd, expected):
    assert packet.contact_email(jd) == expected


def test_message_id_is_deterministic():
    assert packet.message_id_for(12, 3) == "<aja-12-3@ai-job-agent>"


def test_the_packet_has_what_the_human_applies_with():
    app = {"id": 5, "job_id": 9, "title": "AI Engineer", "company": "TestCo",
           "location": "Pune", "apply_link": "https://x/apply", "match_score": 31.5,
           "needs_review": True}
    approval = {"id": 2, "application_version": 1, "decided_at": "2026-11-23T10:00:00Z"}
    artifacts = {
        "resume_docx": {"path": "/a/resume.docx", "sha256": "r", "report": {"ok": False}},
        "cover_letter_docx": {"path": "/a/cl.docx", "sha256": "c", "report": {"ok": True}},
        "cover_letter_txt": {"path": "/a/cl.txt", "sha256": "t", "report": {"ok": True}},
        "jd_requirements": {"path": "/a/jd.json", "sha256": "j", "report": None}}
    body = packet.build_packet(
        app=app, approval=approval, artifacts=artifacts,
        explanation={"strong_matches": ["RAG"], "likely_matches": ["LLM"],
                     "missing_skills": ["Kubernetes"]},
        jd_requirements={"role_summary": "Builds RAG.", "must_have_skills": ["RAG"]},
        contact=None)
    assert body["job"]["apply_link"] == "https://x/apply"
    assert body["job"]["summary"] == "Builds RAG."
    assert body["why_match"] == {"strong_matches": ["RAG"], "likely_matches": ["LLM"]}
    assert body["gaps"] == ["Kubernetes"]
    assert set(body["documents"]) == {"resume_docx", "cover_letter_docx", "cover_letter_txt"}
    assert body["validator"]["needs_review"] is True
    assert "Nothing has been sent or submitted" in body["next_step"]
    assert (body["version"], body["approval_id"]) == (1, 2)


def test_the_email_body_is_the_approved_letter_in_a_fixed_template():
    body = packet.email_body(["P1.", "P2."], {"name": "N", "email": "n@x", "phone": "1"})
    assert body == ("Dear Hiring Team,\n\nP1.\n\nP2.\n\nMy resume and cover letter "
                    "are attached.\n\nSincerely,\nN\nn@x\n1\n")
