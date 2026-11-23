"""The resume_tailor / cover_letter CLIs and their UI entry points
(tailor_to_job, generate_for_job) produce exactly the files, return values
and console output they did before the resume engine was split into steps.

The golden files under tests/golden/resume_engine/ were written by the
pre-refactor code with the fake LLM (LLM_PROVIDER=fake) and these inputs:

    GOLDEN_UPDATE=1 pytest tests/test_resume_engine_golden.py

Node is intercepted: the ".docx" each scenario writes holds the exact JSON
the Python side hands to the docx generator (the generator itself is
unchanged JavaScript, and a real .docx embeds its creation time, so its
bytes differ on every run anyway). Comparisons fold CRLF to LF: text-mode
writes translate newlines on Windows, and the goldens must hold on Linux CI.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.resume_engine import cover_letter, resume_tailor

GOLDEN = Path(__file__).resolve().parent / "golden" / "resume_engine"
UPDATE = bool(os.environ.get("GOLDEN_UPDATE"))

JOBS = [
    {"title": "AI Engineer", "company": "TestCo", "location": "Remote",
     "clean_description": "Build RAG pipelines with FAISS and LLM tooling. "
                          "Ship Python services; PyTorch a plus.",
     "tags": ["Engineering"], "apply_link": "https://x/1"},
    {"title": "Computer Vision Engineer", "company": "VisionCo",
     "location": "Bengaluru",
     "clean_description": "Train GANs and evaluate with FID. OpenCV, CLIP.",
     "tags": [], "apply_link": "https://x/2"},
]
RANKED = [
    {"title": "AI Engineer", "company": "TestCo", "location": "Remote",
     "match_score": 88.5, "score_breakdown": {"skill_overlap": 0.9},
     "strong_matches": ["RAG", "FAISS"], "likely_matches": [],
     "missing_skills": ["PyTorch"], "apply_link": "https://x/1"},
    {"title": "Computer Vision Engineer", "company": "VisionCo",
     "location": "Bengaluru", "match_score": 71.0,
     "score_breakdown": {"skill_overlap": 0.6}, "strong_matches": ["GANs"],
     "likely_matches": [], "missing_skills": [], "apply_link": "https://x/2"},
]


@pytest.fixture
def workspace(tmp_path, monkeypatch, profile, fake_llm):
    data = tmp_path / "data"
    data.mkdir()
    (data / "jobs.json").write_text(json.dumps(JOBS), encoding="utf-8")
    (data / "ranked_jobs.json").write_text(json.dumps(RANKED), encoding="utf-8")
    profile_file = data / "candidate_profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    for module in (resume_tailor, cover_letter):
        monkeypatch.setattr(module, "DATA_DIR", data)
        monkeypatch.setattr(module, "PROFILE_FILE", profile_file)
        monkeypatch.setattr(module, "RANKED_FILE", data / "ranked_jobs.json")

    node = {"fail": False}
    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if not (isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "node"):
            return real_run(cmd, *args, **kwargs)
        if node["fail"]:
            return subprocess.CompletedProcess(cmd, 1, "", "Error: boom from docx")
        Path(cmd[3]).write_bytes(Path(cmd[2]).read_bytes())
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return {"data": data, "node": node, "fake_llm": fake_llm}


def _norm(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _outputs(data_dir: Path) -> dict[str, bytes]:
    inputs = {"jobs.json", "ranked_jobs.json", "candidate_profile.json"}
    return {p.name: _norm(p.read_bytes()) for p in sorted(data_dir.iterdir())
            if p.name not in inputs}


def _portable(value, data_dir: Path):
    text = json.dumps(value, default=str, ensure_ascii=False, indent=2)
    for form in (str(data_dir), str(data_dir).replace("\\", "\\\\")):
        text = text.replace(form, "<DATA>")
    return text.replace("\\\\", "/")


def _check(name: str, data_dir: Path, stdout: str, returned=None):
    actual = {"stdout.txt": _norm(stdout.replace(str(data_dir), "<DATA>")
                                  .replace("\\", "/").encode("utf-8")),
              **_outputs(data_dir)}
    if returned is not None:
        actual["returned.json"] = _norm(_portable(returned, data_dir).encode("utf-8"))
    folder = GOLDEN / name
    if UPDATE:
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.iterdir():
            old.unlink()
        for fname, data in actual.items():
            (folder / fname).write_bytes(data)
        return
    expected = {p.name: _norm(p.read_bytes()) for p in sorted(folder.iterdir())}
    assert sorted(actual) == sorted(expected)
    for fname in expected:
        assert actual[fname] == expected[fname], f"{name}/{fname} differs"


def _run_cli(main, argv, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog", *argv])
    code = None
    try:
        main()
    except SystemExit as exc:
        code = exc.code
    return capsys.readouterr().out + (f"[exit {code}]\n" if code is not None else "")


def _fabricate_one_project(fake_llm):
    def answer(prompt, default):
        if "GAN pipelines" in prompt:
            return {"tailored_bullets": default["tailored_bullets"]
                    + ["Scaled training on Databricks to 50,000 images."]}
        return default
    fake_llm.on("tailor_bullets", answer)


# ---- resume_tailor ------------------------------------------------------------------

def test_resume_tailor_cli(workspace, monkeypatch, capsys):
    out = _run_cli(resume_tailor.main, ["--rank", "1"], monkeypatch, capsys)
    _check("resume_tailor_cli_rank1", workspace["data"], out)


def test_resume_tailor_cli_with_a_fabricating_model(workspace, monkeypatch, capsys):
    _fabricate_one_project(workspace["fake_llm"])
    out = _run_cli(resume_tailor.main, ["--rank", "2"], monkeypatch, capsys)
    _check("resume_tailor_cli_rank2_flagged", workspace["data"], out)


def test_resume_tailor_cli_when_the_docx_generator_fails(workspace, monkeypatch,
                                                         capsys):
    workspace["node"]["fail"] = True
    out = _run_cli(resume_tailor.main, ["--rank", "1"], monkeypatch, capsys)
    _check("resume_tailor_cli_docx_fails", workspace["data"], out)


def test_tailor_to_job_entry_point(workspace, profile, capsys):
    _fabricate_one_project(workspace["fake_llm"])
    job = {**RANKED[0], "clean_description": JOBS[0]["clean_description"]}
    returned = resume_tailor.tailor_to_job(profile, job, job["clean_description"])
    _check("tailor_to_job", workspace["data"], capsys.readouterr().out, returned)


# ---- cover_letter -------------------------------------------------------------------

def test_cover_letter_cli(workspace, monkeypatch, capsys):
    out = _run_cli(cover_letter.main, ["--rank", "1"], monkeypatch, capsys)
    _check("cover_letter_cli_rank1", workspace["data"], out)


def test_cover_letter_cli_txt_only(workspace, monkeypatch, capsys):
    out = _run_cli(cover_letter.main, ["--rank", "2", "--txt-only"], monkeypatch, capsys)
    _check("cover_letter_cli_rank2_txt_only", workspace["data"], out)


def test_cover_letter_cli_with_unverified_claims(workspace, monkeypatch, capsys):
    workspace["fake_llm"].on("cover_letter", lambda prompt, default: {
        "paragraphs": [default["paragraphs"][0],
                       "I shipped Kubernetes clusters serving 2 million users.",
                       default["paragraphs"][2]]})
    out = _run_cli(cover_letter.main, ["--rank", "1"], monkeypatch, capsys)
    _check("cover_letter_cli_flagged", workspace["data"], out)


def test_cover_letter_cli_when_the_docx_generator_fails(workspace, monkeypatch,
                                                        capsys):
    workspace["node"]["fail"] = True
    out = _run_cli(cover_letter.main, ["--rank", "1"], monkeypatch, capsys)
    _check("cover_letter_cli_docx_fails", workspace["data"], out)


@pytest.mark.parametrize("make_docx,node_fails", [(True, False), (False, False),
                                                  (True, True)],
                         ids=["docx", "txt_only", "docx_fails"])
def test_generate_for_job_entry_point(workspace, profile, capsys, make_docx,
                                      node_fails):
    workspace["node"]["fail"] = node_fails
    workspace["fake_llm"].on("cover_letter", lambda prompt, default: {
        "paragraphs": default["paragraphs"][:2] + ["Reached 99% accuracy."]})
    job = {**RANKED[0]}
    returned = cover_letter.generate_for_job(
        profile, job, JOBS[0]["clean_description"], make_docx=make_docx)
    _check(f"generate_for_job_{'docx' if make_docx else 'txt'}"
           + ("_fails" if node_fails else ""),
           workspace["data"], capsys.readouterr().out, returned)


def test_llm_calls_per_cli_run(workspace, monkeypatch, capsys):
    """The CLIs' LLM call pattern is part of their behaviour: per-project
    tailoring plus one skills-ordering call, one extraction per document."""
    _run_cli(resume_tailor.main, ["--rank", "1"], monkeypatch, capsys)
    assert dict(workspace["fake_llm"].calls) == {
        "extract_requirements": 1, "tailor_bullets": 2, "order_skills": 1}
    workspace["fake_llm"].calls.clear()
    _run_cli(cover_letter.main, ["--rank", "1"], monkeypatch, capsys)
    assert dict(workspace["fake_llm"].calls) == {"extract_requirements": 1,
                                                 "cover_letter": 1}
