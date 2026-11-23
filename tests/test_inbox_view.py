"""The Streamlit Inbox (backend/ui/views/inbox.py): what a card shows, and
that Approve / Decline / Edit go through decisions.submit_decision() with
the version on screen. The agent's store is stubbed (no database).

Streamlit is skipped per test, never at module level (the slim CI install
has no Streamlit; see test_jobs_view.py)."""
import pytest

ARTIFACTS = {
    "resume_docx": {"kind": "resume_docx", "path": "/nope/resume.docx", "sha256": "r",
                    "report": {"ok": False, "projects": [
                        {"project": "RAG Medical Copilot", "ok": False,
                         "flagged": ["Databricks"]},
                        {"project": "GAN Image Synthesizer", "ok": True, "flagged": []}]}},
    "cover_letter_docx": {"kind": "cover_letter_docx", "path": "/nope/cl.docx",
                          "sha256": "c", "report": {"ok": False, "paragraphs": [
                              {"paragraph": 2, "flags": ["Google", "12"]}]}},
}
DOCUMENTS = {"cover_letter_paragraphs": ["One.", "Two at Google with 12 people.", "Three."],
             "resume_projects": [{"title": "RAG Medical Copilot", "bullets": ["Built RAG."]},
                                 {"title": "GAN Image Synthesizer",
                                  "bullets": ["Built GANs.", "Measured FID."]}],
             "skills": ["RAG"], "edit_flags": []}
APPS = [{"id": 1, "title": "AI Engineer", "company": "TestCo", "location": "Pune",
         "match_score": 31.4, "version": 1, "needs_review": True,
         "apply_link": "https://x/apply", "job_id": 9, "profile_hash": "p", "cache_key": "k"},
        {"id": 2, "title": "ML Engineer", "company": "OtherCo", "location": "Remote - India",
         "match_score": 22.0, "version": 3, "needs_review": False, "apply_link": None,
         "job_id": 8, "profile_hash": "p", "cache_key": "k"}]


def test_flag_lines_and_flagged_paragraphs():
    pytest.importorskip("streamlit")
    from backend.ui.views import inbox
    assert inbox.flag_lines(ARTIFACTS, DOCUMENTS) == [
        "Resume, RAG Medical Copilot: unverified Databricks (original bullets used)",
        "Cover letter, paragraph 2: unverified Google, 12"]
    assert inbox.flagged_paragraphs(ARTIFACTS) == {2}
    assert inbox.flag_lines({}, None) == []


def test_edits_from_form_keeps_only_what_changed():
    pytest.importorskip("streamlit")
    from backend.ui.views import inbox
    same = inbox.edits_from_form(DOCUMENTS, DOCUMENTS["cover_letter_paragraphs"],
                                 {"RAG Medical Copilot": "Built RAG.",
                                  "GAN Image Synthesizer": "Built GANs.\nMeasured FID."})
    assert same == {}
    edits = inbox.edits_from_form(DOCUMENTS, ["One.", "", "Three, revised."],
                                  {"RAG Medical Copilot": " Built  RAG. \n\nShipped it.",
                                   "GAN Image Synthesizer": "Built GANs.\nMeasured FID."})
    assert edits == {"cover_letter_paragraphs": ["One.", "Three, revised."],
                     "resume_bullets": {"RAG Medical Copilot": ["Built RAG.", "Shipped it."]}}


@pytest.fixture
def stubbed(monkeypatch):
    pytest.importorskip("streamlit")
    from backend.agent import application_graph, decisions, store
    calls = []
    monkeypatch.setattr(store, "list_applications", lambda **kw: list(APPS))
    monkeypatch.setattr(store, "get_artifacts",
                        lambda app_id: list(ARTIFACTS.values()) if app_id == 1 else [])
    monkeypatch.setattr(decisions, "explanation", lambda app: {"explanation": {
        "strong_matches": ["RAG"], "likely_matches": [], "missing_skills": ["Kubernetes"]}})
    monkeypatch.setattr(decisions, "list_approvals", lambda app_id: [])
    monkeypatch.setattr(application_graph, "current_state", lambda app_id: {"x": 1})
    monkeypatch.setattr(application_graph, "documents", lambda state: DOCUMENTS)

    def submit(application_id, **kwargs):
        calls.append((application_id, kwargs))
        return {"approval_id": 1}
    monkeypatch.setattr(decisions, "submit_decision", submit)
    from backend.ui.views import inbox
    monkeypatch.setattr(inbox, "_load_memories", lambda profile: (1, []))   # Phase 6
    monkeypatch.setattr(inbox, "_load_follow_ups", lambda profile: [])
    return calls


def _view():
    from backend.ui.views import inbox
    inbox.render({"name": "Test"})


def _run():
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_view)
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def _markdown(at):
    return "\n".join(m.value for m in at.markdown)


def test_the_inbox_lists_awaiting_applications_with_their_flags(stubbed):
    at = _run()
    text = _markdown(at)
    assert "**2** applications awaiting" in text
    assert text.index("AI Engineer") < text.index("ML Engineer")    # as listed: by score
    assert "Needs review" in text and "Validated" in text
    assert "Resume, RAG Medical Copilot: unverified Databricks" in text
    assert "Kubernetes" in text                                      # the gaps
    assert "border-left:4px solid" in text                           # flagged paragraph


def test_approve_and_decline_submit_the_version_on_screen(stubbed):
    at = _run()
    at.button(key="approve-1-1").click().run(timeout=30)
    assert stubbed[-1] == (1, {"action": "approve", "version": 1})
    at.text_input(key="reason-2-3").input("Not my field")
    at.button(key="decline-2-3").click().run(timeout=30)
    assert stubbed[-1] == (2, {"action": "decline", "version": 3, "reason": "Not my field"})


def test_a_flagged_edit_shows_the_flags(stubbed, monkeypatch):
    from backend.agent import decisions

    def flagged(application_id, **kwargs):
        raise decisions.FlaggedEdit([{"document": "cover_letter", "paragraph": 3,
                                      "flags": ["Google"]}])
    monkeypatch.setattr(decisions, "submit_decision", flagged)
    at = _run()
    at.text_area(key="p3-1-1").input("Three, now at Google.")
    at.button(key="FormSubmitter:edit-1-1-Save edit").click().run(timeout=30)
    assert any("paragraph 3: Google" in e.value for e in at.error)


def test_a_pending_decision_hides_the_buttons(stubbed, monkeypatch):
    from backend.agent import decisions
    monkeypatch.setattr(decisions, "list_approvals", lambda app_id: [
        {"decision": "approve", "application_version": 1 if app_id == 1 else 2}])
    at = _run()
    keys = {b.key for b in at.button}
    assert "approve-1-1" not in keys and "approve-2-3" in keys
    assert any("recorded; the worker is applying it" in i.value for i in at.info)


def test_without_a_database_the_inbox_explains(monkeypatch):
    pytest.importorskip("streamlit")
    from backend.agent import store

    def down(**kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(store, "list_applications", down)
    at = _run()
    assert "isn't reachable" in _markdown(at)


# ---- Memory (Phase 6) ----------------------------------------------------------------

MEMORIES = [
    {"id": 7, "kind": "company_avoid", "value": "acme", "provenance": "learned",
     "weight": 2.0, "active": True, "evidence": {
         "rule": "declined 2 times (>= 2), never approved",
         "declined": [{"decision": "decline", "approval_id": 3, "application_id": 11,
                       "event_id": 21, "title": "AI Engineer", "company": "acme",
                       "reason": "too far"},
                      {"decision": "decline", "approval_id": 4, "application_id": 12,
                       "event_id": None, "title": "ML Engineer", "company": "acme",
                       "reason": None}],
         "approved": []}},
    {"id": 8, "kind": "title_prefer", "value": "machine learning", "provenance": "stated",
     "weight": 3.0, "active": False, "evidence": {"stated_via": "api"}},
]


def test_memory_label_and_evidence_lines():
    pytest.importorskip("streamlit")
    from backend.ui.views import inbox
    assert inbox.memory_label(MEMORIES[0]) == "Avoid company: acme · learned · weight 2"
    assert inbox.memory_label(MEMORIES[1]) == (
        "Prefer title word: machine learning · stated by you · weight 3 · inactive")
    assert inbox.evidence_lines(MEMORIES[0]) == [
        "Learned: declined 2 times (>= 2), never approved.",
        'Declined application #11: AI Engineer at acme (approval #3, event #21) -- "too far"',
        "Declined application #12: ML Engineer at acme (approval #4)"]
    assert inbox.evidence_lines(MEMORIES[1]) == ["You stated this (via api)."]
    note = {"id": 9, "kind": "note", "value": "x", "provenance": "stated", "weight": 2.0,
            "active": True, "evidence": {}}
    assert inbox.memory_label(note) == "Note: x · stated by you"


@pytest.fixture
def memories(stubbed, monkeypatch):
    from backend.agent import memory
    from backend.ui.views import inbox
    calls = []
    monkeypatch.setattr(inbox, "_load_memories", lambda profile: (5, list(MEMORIES)))
    monkeypatch.setattr(memory, "update", lambda **kw: calls.append(("update", kw)))
    monkeypatch.setattr(memory, "delete", lambda **kw: calls.append(("delete", kw)))
    monkeypatch.setattr(memory, "create_stated",
                        lambda **kw: calls.append(("create_stated", kw)))
    return calls


def test_the_memory_section_lists_provenance_and_evidence(memories):
    text = _markdown(_run())
    assert "#### Memory" in text
    assert "Avoid company: acme · learned · weight 2" in text
    assert "uja-pill amber" in text and "uja-pill green" in text
    assert "too far" in text and "approval #3, event #21" in text


def test_deactivate_activate_and_delete(memories):
    at = _run()
    at.button(key="mem-7-toggle").click().run(timeout=30)
    assert memories[-1] == ("update", {"user_id": 5, "memory_id": 7, "active": False})
    at.button(key="mem-8-toggle").click().run(timeout=30)
    assert memories[-1] == ("update", {"user_id": 5, "memory_id": 8, "active": True})
    at.button(key="mem-7-delete").click().run(timeout=30)
    assert memories[-1] == ("delete", {"user_id": 5, "memory_id": 7})


def test_edit_and_add(memories):
    at = _run()
    at.text_input(key="mem-7-value").input("Acme Corp")
    at.number_input(key="mem-7-weight").set_value(4.0)
    at.button(key="FormSubmitter:mem-7-form-Save").click().run(timeout=30)
    assert memories[-1] == ("update", {"user_id": 5, "memory_id": 7, "value": "Acme Corp",
                                       "weight": 4.0})
    at.selectbox(key="mem-add-kind").set_value("location_prefer")
    at.text_input(key="mem-add-value").input("Pune")
    at.button(key="FormSubmitter:mem-add-Remember").click().run(timeout=30)
    assert memories[-1] == ("create_stated", {"user_id": 5, "kind": "location_prefer",
                                              "value": "Pune", "via": "ui"})


def test_a_memory_error_is_shown_not_raised(memories, monkeypatch):
    from backend.agent import memory

    def clash(**kw):
        raise memory.MemoryConflict("You already have a company_avoid memory for 'acme'.")
    monkeypatch.setattr(memory, "create_stated", clash)
    at = _run()
    at.text_input(key="mem-add-value").input("acme")
    at.button(key="FormSubmitter:mem-add-Remember").click().run(timeout=30)
    assert any("already have" in w.value for w in at.warning)


# ---- Follow-ups due (Phase 6) --------------------------------------------------------

DUE = [{"id": 4, "title": "AI Engineer", "company": "TestCo", "location": "Pune",
        "apply_link": "https://x/4", "follow_up_draft": {"status": "done",
                                                         "external_id": "r-1"}},
       {"id": 5, "title": "ML Engineer", "company": "OtherCo", "location": None,
        "apply_link": None, "follow_up_draft": None}]


def test_follow_up_line():
    pytest.importorskip("streamlit")
    from backend.ui.views import inbox
    assert inbox.follow_up_line(DUE[0]) == (
        "AI Engineer at TestCo (Pune) -- a follow-up Gmail draft is ready (not sent)")
    assert inbox.follow_up_line(DUE[1]) == (
        "ML Engineer at OtherCo (location n/a) -- no draft (no contact email in the "
        "posting, or Gmail is off)")
    assert "failed" in inbox.follow_up_line({**DUE[0], "follow_up_draft": {"status": "failed"}})


def test_the_inbox_lists_the_follow_ups_due(stubbed, monkeypatch):
    from backend.ui.views import inbox
    monkeypatch.setattr(inbox, "_load_follow_ups", lambda profile: list(DUE))
    text = _markdown(_run())
    assert "#### Follow-ups due (2)" in text
    assert "AI Engineer at TestCo (Pune) -- a follow-up Gmail draft is ready" in text
    assert text.index("Follow-ups due") < text.index("#### Memory")


def test_no_follow_ups_due_shows_no_section(stubbed):
    assert "Follow-ups due" not in _markdown(_run())
