"""A person's edits to drafted documents (backend/agent/edits.py): applied,
re-validated with the truthfulness validators, flags reported only for the
edited text, and a flagged human edit kept as written. No LLM."""
import pytest

from backend.agent.edits import InvalidEdit, apply_edits, normalize


@pytest.fixture
def state(profile):
    job = {"title": "AI Engineer", "company": "TestCo"}
    bullets = [b.strip() for b in profile["projects"][0]["description"].split("•")
               if b.strip()]
    return {
        "profile": profile, "job": job,
        "paragraphs": ["The candidate is applying to TestCo with project evidence.",
                       "The strongest evidence is the RAG Medical Copilot project.",
                       "The candidate is available immediately."],
        "resume_content": {"projects": [
            {"title": "RAG Medical Copilot", "duration": "", "bullets": bullets},
            {"title": "GAN Image Synthesizer", "duration": "", "bullets": ["Built GANs."]}],
            "skills": ["RAG"]},
        "resume_report": [{"project": "RAG Medical Copilot", "ok": True, "flagged": []},
                          {"project": "GAN Image Synthesizer", "ok": True, "flagged": []}],
        "letter_review": [],
    }


def test_an_unflagged_paragraph_edit(state):
    new = list(state["paragraphs"])
    new[2] = "The candidate can start right away and thanks the team."
    result = apply_edits(state, {"cover_letter_paragraphs": new})
    assert result.flags == [] and result.letter_review == []
    assert result.paragraphs == new
    assert result.changed == {"cover_letter_paragraphs": [3]}
    assert state["paragraphs"][2] != new[2]            # the input is not mutated


def test_a_flagged_paragraph_edit_reports_only_the_edited_paragraph(state):
    state["letter_review"] = [{"paragraph": 1, "flags": ["Old"]}]
    new = list(state["paragraphs"])
    new[1] = "I led a Kubernetes team of 12 at Google."
    result = apply_edits(state, {"cover_letter_paragraphs": new})
    [flag] = result.flags
    assert flag["document"] == "cover_letter" and flag["paragraph"] == 2
    assert {"Kubernetes", "Google", "12"} <= set(flag["flags"])
    assert result.paragraphs[1] == new[1]              # kept as written


def test_a_flagged_bullet_edit_is_kept_not_reverted(state):
    bullets = ["Deployed the RAG pipeline on Databricks for 40,000 clinicians."]
    result = apply_edits(state, {"resume_bullets": {"rag medical copilot": bullets}})
    [flag] = result.flags
    assert flag == {"document": "resume", "project": "RAG Medical Copilot",
                    "flags": sorted(flag["flags"])}
    assert {"Databricks", "40000"} <= set(flag["flags"])
    project = result.resume_content["projects"][0]
    assert project["bullets"] == bullets               # no fallback for a human edit
    entry = result.resume_report[0]
    assert (entry["ok"], entry["edited"]) == (False, True)
    assert result.resume_report[1]["ok"] is True       # the other project untouched


def test_an_unflagged_bullet_edit(state):
    bullets = ["Deployed a Retrieval-Augmented Generation pipeline for clinical Q&A "
               "using FAISS and Sentence Transformers."]
    result = apply_edits(state, {"resume_bullets": {"RAG Medical Copilot": bullets}})
    assert result.flags == []
    assert result.changed["resume_projects"] == ["RAG Medical Copilot"]


@pytest.mark.parametrize("edits", [
    None, {}, {"subject": "x"}, {"cover_letter_paragraphs": []},
    {"cover_letter_paragraphs": ["ok", "  "]}, {"cover_letter_paragraphs": "text"},
    {"resume_bullets": {}}, {"resume_bullets": {"RAG Medical Copilot": []}},
    {"cover_letter_paragraphs": ["x"] * 9},
])
def test_malformed_edits(edits):
    with pytest.raises(InvalidEdit):
        normalize(edits)


def test_an_unknown_project_or_a_no_op_edit_is_refused(state):
    with pytest.raises(InvalidEdit, match="no project"):
        apply_edits(state, {"resume_bullets": {"Nope": ["Built it."]}})
    with pytest.raises(InvalidEdit, match="change nothing"):
        apply_edits(state, {"cover_letter_paragraphs": state["paragraphs"]})


def test_dropping_a_paragraph_is_a_change(state):
    result = apply_edits(state, {"cover_letter_paragraphs": state["paragraphs"][:2]})
    assert result.paragraphs == state["paragraphs"][:2] and result.flags == []
