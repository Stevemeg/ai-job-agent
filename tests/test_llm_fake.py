"""The fake LLM provider (LLM_PROVIDER=fake): selected only by name,
deterministic, recognises every engine prompt, and honours its sleep /
raise / log controls."""
import json
import time

import pytest

from backend.llm import LLMUnavailableError, fake_provider, generate, provider
from backend.resume_engine import cover_letter, resume_tailor


@pytest.fixture
def clean_provider(monkeypatch):
    for var in ("LLM_PROVIDER", "GROQ_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    provider.reset_provider()
    yield
    provider.reset_provider()


def test_llm_provider_selects_the_fake_only_by_name(clean_provider, monkeypatch):
    assert provider.get_provider().name == "ollama"          # auto, no key
    provider.reset_provider()
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert provider.get_provider().name == "groq"            # auto, key
    provider.reset_provider()
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert provider.get_provider().name == "fake"
    provider.reset_provider()
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    assert provider.get_provider().name == "ollama"          # key ignored
    provider.reset_provider()
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(ValueError):
        provider.get_provider()
    provider.reset_provider()
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.delenv("GROQ_API_KEY")
    with pytest.raises(LLMUnavailableError):
        provider.get_provider()


def test_every_engine_step_is_answered_deterministically(fake_llm, profile):
    jd = "Build RAG pipelines with FAISS and PyTorch on AWS. Python and SQL."
    req = resume_tailor.extract_jd_requirements(jd, None)
    assert req == resume_tailor.extract_jd_requirements(jd, None)
    assert {"role_summary", "must_have_skills", "keywords_to_include"} <= set(req)
    assert "RAG" in req["must_have_skills"]

    project = profile["projects"][0]
    bullets = resume_tailor.tailor_project_bullets(project, req, None)
    assert bullets == [b.strip() for b in project["description"].split("•")
                       if b.strip()]                        # the originals
    skills = resume_tailor.order_skills_for_role(profile["skills"], req, None)
    assert skills == sorted((s["skill"] for s in profile["skills"]), key=str.lower)

    job = {"title": "AI Engineer", "company": "TestCo"}
    paragraphs = cover_letter.draft_cover_letter(profile, job, req, None)
    assert len(paragraphs) == 3 and "TestCo" in paragraphs[0]
    assert "RAG Medical Copilot" in paragraphs[1]
    assert dict(fake_llm.calls) == {"extract_requirements": 2, "tailor_bullets": 1,
                                    "order_skills": 1, "cover_letter": 1}


def test_an_unknown_prompt_is_an_error(fake_llm):
    with pytest.raises(fake_provider.UnknownPrompt):
        generate("Write me a poem.")


def test_overrides_replace_one_steps_answer(fake_llm, profile):
    fake_llm.on("tailor_bullets", lambda prompt, answer: {
        "tailored_bullets": answer["tailored_bullets"] + ["Ran it on Databricks."]})
    bullets = resume_tailor.tailor_project_bullets(
        profile["projects"][0], {"keywords_to_include": []}, None)
    assert bullets[-1] == "Ran it on Databricks."
    with pytest.raises(ValueError):
        fake_llm.on("no_such_step", lambda p, a: a)


@pytest.mark.parametrize("mode,outcome", [
    ("unavailable", LLMUnavailableError), ("error", RuntimeError), ("none", None)])
def test_raise_modes(fake_llm, monkeypatch, mode, outcome):
    monkeypatch.setenv("FAKE_LLM_RAISE", f"extract_requirements={mode}")
    if outcome is None:
        assert resume_tailor.extract_jd_requirements("jd", None) is None
    else:
        with pytest.raises(outcome):
            resume_tailor.extract_jd_requirements("jd", None)
    assert fake_llm.calls["extract_requirements"] == 1      # counted anyway


def test_sleep_and_the_cross_process_call_log(fake_llm, monkeypatch, tmp_path):
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LLM_LOG", str(log))
    monkeypatch.setenv("FAKE_LLM_SLEEP", "extract_requirements=0.3")
    started = time.monotonic()
    resume_tailor.extract_jd_requirements("jd", None)
    assert time.monotonic() - started >= 0.25          # (Windows timer granularity)
    resume_tailor.extract_jd_requirements("jd", None)
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert [(x["step"], x["n"]) for x in lines] == [("extract_requirements", 1),
                                                   ("extract_requirements", 2)]
    assert fake_provider.read_call_log(log) == {"extract_requirements": 2}
    assert fake_provider.read_call_log(tmp_path / "missing") == {}
