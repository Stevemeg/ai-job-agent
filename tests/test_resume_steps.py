"""The resume engine's steps, called one at a time as the agent graph does:
the one-call tailoring and its fallbacks, validation of a precomputed
tailoring, the output-path parameters, and real .docx rendering (when Node
and the docx package are installed)."""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from backend.resume_engine import cover_letter, resume_tailor

JD = {"role_summary": "Builds RAG systems.", "must_have_skills": ["RAG"],
      "nice_to_have_skills": [], "key_responsibilities": ["Ship"],
      "keywords_to_include": ["RAG", "FAISS"]}
JOB = {"title": "AI Engineer", "company": "TestCo"}


def _originals(project):
    return [b.strip() for b in project["description"].split("•") if b.strip()]


def test_single_call_tailoring_is_one_llm_call(fake_llm, profile):
    tailored = resume_tailor.tailor_resume_single_call(profile, JD, None)
    assert dict(fake_llm.calls) == {"tailor_resume": 1}
    assert tailored["bullets"] == [_originals(p) for p in profile["projects"]]
    assert sorted(tailored["ordered_skills"]) == sorted(
        s["skill"] for s in profile["skills"])

    content, report = resume_tailor.build_tailored_resume(
        profile, JOB, JD, None, tailored=tailored, verbose=False)
    assert dict(fake_llm.calls) == {"tailor_resume": 1}     # validation: no call
    assert [r["ok"] for r in report] == [True, True]
    assert content["skills"] == tailored["ordered_skills"]
    assert content["target_company"] == "TestCo"
    assert content["jd_summary"] == "Builds RAG systems."


def test_single_call_without_requirements_makes_no_call(fake_llm, profile):
    tailored = resume_tailor.tailor_resume_single_call(profile, None, None)
    assert fake_llm.calls == {}
    assert tailored["ordered_skills"] == [s["skill"] for s in profile["skills"]]


@pytest.mark.parametrize("answer", [
    None,
    {"projects": [], "ordered_skills": ["RAG"]},             # nothing usable
    {"projects": [{"title": "x", "tailored_bullets": "not a list"}]},
    {"projects": "nope", "ordered_skills": "nope"},
])
def test_single_call_falls_back_to_the_originals(fake_llm, profile, answer):
    fake_llm.on("tailor_resume", lambda prompt, default: answer)
    tailored = resume_tailor.tailor_resume_single_call(profile, JD, None)
    assert tailored["bullets"] == [_originals(p) for p in profile["projects"]]
    assert tailored["ordered_skills"] == [s["skill"] for s in profile["skills"]]


@pytest.mark.parametrize("entries,expected", [
    # by title, whatever the order
    ([("SECOND", ["S."]), ("FIRST", ["F."])], [["F."], ["S."]]),
    # retitled by the model: by position
    ([("renamed a", ["A."]), ("renamed b", ["B."])], [["A."], ["B."]]),
    # position 0 holds the second project's answer: the first gets nothing
    # from it (originals), the second finds its own by title
    ([("SECOND", ["S."]), ("renamed", ["R."])], ["orig", ["S."]]),
], ids=["by_title", "by_position", "never_across_projects"])
def test_single_call_matches_projects_by_title_then_position(fake_llm, profile,
                                                             entries, expected):
    first, second = profile["projects"]
    titles = {"FIRST": first["title"].upper(), "SECOND": second["title"].upper()}
    fake_llm.on("tailor_resume", lambda prompt, default: {"projects": [
        {"title": titles.get(t, t), "tailored_bullets": b} for t, b in entries]})
    tailored = resume_tailor.tailor_resume_single_call(profile, JD, None)
    assert tailored["bullets"] == [_originals(first) if e == "orig" else e
                                   for e in expected]


def test_a_fabricated_single_call_project_falls_back_and_is_reported(fake_llm,
                                                                     profile):
    def fabricate(prompt, default):
        default["projects"][1]["tailored_bullets"].append(
            "Scaled it on Databricks for 50,000 users.")
        return default
    fake_llm.on("tailor_resume", fabricate)
    tailored = resume_tailor.tailor_resume_single_call(profile, JD, None)
    content, report = resume_tailor.build_tailored_resume(
        profile, JOB, JD, None, tailored=tailored, verbose=False)
    assert report[0]["ok"] and not report[1]["ok"]
    assert {"Databricks", "50000"} <= set(report[1]["flagged"])
    assert content["projects"][1]["bullets"] == _originals(profile["projects"][1])


def test_verbose_false_prints_nothing(fake_llm, profile, capsys):
    tailored = resume_tailor.tailor_resume_single_call(profile, JD, None)
    resume_tailor.build_tailored_resume(profile, JOB, JD, None, tailored=tailored,
                                        verbose=False)
    assert capsys.readouterr().out == ""


@pytest.fixture
def node_writes_payload(monkeypatch):
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "node":
            Path(cmd[3]).write_bytes(Path(cmd[2]).read_bytes())
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", fake_run)


def test_output_paths_are_parameters(fake_llm, profile, tmp_path, monkeypatch,
                                     node_writes_payload):
    shared = tmp_path / "shared"
    shared.mkdir()
    monkeypatch.setattr(resume_tailor, "DATA_DIR", shared)
    monkeypatch.setattr(cover_letter, "DATA_DIR", shared)
    own = tmp_path / "applications" / "7"
    own.mkdir(parents=True)

    path, _ = resume_tailor.tailor_to_job(profile, JOB, "jd", output_path=own / "resume.docx")
    assert path == own / "resume.docx" and path.exists()
    path, _, _ = cover_letter.generate_for_job(profile, JOB, "jd", output_dir=own)
    assert path.parent == own and path.suffix == ".docx"
    assert (own / path.with_suffix(".txt").name).exists()
    assert list(shared.iterdir()) == []                       # nothing shared


def test_render_cover_letter_text_matches_the_cli_letter(profile, tmp_path):
    out = cover_letter.render_cover_letter(["One.", "Two."], tmp_path / "l.txt",
                                           profile, JOB)
    assert out.read_text(encoding="utf-8") == cover_letter.cover_letter_text(
        ["One.", "Two."], profile, JOB)
    assert out.read_text(encoding="utf-8").splitlines()[4] == "Re: AI Engineer -- TestCo"


def _node_with_docx() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(["node", "-e", "require('docx')"],
                           cwd=Path(resume_tailor.DOCX_SCRIPT).parent,
                           capture_output=True)
    return probe.returncode == 0


@pytest.mark.skipif(not _node_with_docx(),
                    reason="needs node and `npm ci` in backend/resume_engine")
def test_real_docx_rendering(fake_llm, profile, tmp_path):
    tailored = resume_tailor.tailor_resume_single_call(profile, JD, None)
    content, _ = resume_tailor.build_tailored_resume(
        profile, JOB, JD, None, tailored=tailored, verbose=False)
    resume = resume_tailor.render_resume_docx(content, tmp_path / "resume.docx")
    letter = cover_letter.render_cover_letter(
        ["The candidate is applying to TestCo."], tmp_path / "letter.docx",
        profile, JOB)
    for path, needle in ((resume, "RAG Medical Copilot"), (letter, "TestCo")):
        with zipfile.ZipFile(path) as z:
            assert needle in z.read("word/document.xml").decode("utf-8")


def test_a_failing_generator_raises_docx_render_error(profile, tmp_path,
                                                      monkeypatch):
    def failing(cmd, *args, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, "", "Error: x" * 100)
    monkeypatch.setattr(subprocess, "run", failing)
    with pytest.raises(resume_tailor.DocxRenderError) as exc:
        resume_tailor.render_resume_docx({}, tmp_path / "r.docx")
    assert exc.value.stderr == "Error: x" * 100
    assert str(exc.value) == "docx generation failed: " + ("Error: x" * 100)[:400]
    with pytest.raises(resume_tailor.DocxRenderError):
        cover_letter.render_cover_letter([], tmp_path / "l.docx", profile, JOB)
