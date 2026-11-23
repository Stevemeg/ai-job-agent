"""Fake provider -- LLM_PROVIDER=fake. Deterministic and offline, for CI and
the agent's kill/retry tests. Never selected unless asked for by name.

Every prompt the engines send is a fixed template, so each call is filed
under a STEP by a marker phrase in its prompt (STEP_MARKERS). The answer is
a deterministic function of the prompt, shaped like the real model's JSON:

  extract_requirements  JD requirements: tech-looking terms of the JD text
  tailor_bullets        the project's original bullets, unchanged
  order_skills          the candidate's skills, sorted case-insensitively
  tailor_resume         (the agent's one-call tailoring) every project's
                        original bullets + the sorted skills
  cover_letter          three paragraphs that pass the truthfulness
                        validator for single-word company names

Echoing the original bullets means the validator passes by default; a test
that wants a flag overrides the step's answer (CONTROL.on).

Controls -- environment variables (read on every call, so a subprocess
worker is configured by its env) and an in-process test hook:

  FAKE_LLM_SLEEP="tailor_resume=30,cover_letter=0.5"   sleep before answering
  FAKE_LLM_RAISE="extract_requirements=unavailable"    unavailable -> raise
        LLMUnavailableError, error -> RuntimeError, none -> return None
        (unparseable model output)
  FAKE_LLM_LOG=/path/calls.jsonl     append {"step", "pid", "n"} per call,
        flushed and fsynced BEFORE any sleep, so a call in a process that is
        then SIGKILLed is still counted (read it back with read_call_log)

  CONTROL.calls      Counter of this process's calls per step
  CONTROL.on(step, fn)   fn(prompt, default_answer) -> answer (or raise)
  CONTROL.reset()
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from .provider import LLMUnavailableError

# (step, marker): checked in order; the first marker found in the prompt
# names the step. tailor_resume's prompt is checked before tailor_bullets'.
STEP_MARKERS = (
    ("extract_requirements",
     "Extract the key information and return ONLY valid JSON"),
    ("tailor_resume", "Rewrite the bullet points of EACH project below"),
    ("tailor_bullets", "Rewrite the project bullet points below"),
    ("order_skills", "Reorder these skills to put the most"),
    ("cover_letter", "writing a cover letter body"),
)

_TECH_TERM = re.compile(r"\b(?:[A-Z]{2,}[A-Za-z0-9+#.]*|[A-Z][a-z]+[A-Z][A-Za-z0-9]*)\b")


class UnknownPrompt(ValueError):
    """A prompt with no known step marker: an engine prompt changed and the
    fake (and its tests) must learn the new template."""


def classify(prompt: str) -> str:
    for step, marker in STEP_MARKERS:
        if marker in prompt:
            return step
    raise UnknownPrompt("fake LLM: no step marker in prompt: "
                        + prompt[:120].replace("\n", " "))


# ---- deterministic answers --------------------------------------------------
def _between(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    i += len(start)
    j = text.find(end, i)
    return text[i:] if j < 0 else text[i:j]


def _json_after(text: str, label: str) -> Any:
    """The JSON value on the line after `label` (e.g. 'Candidate skills: [...]')."""
    line = _between(text, label, "\n").strip()
    try:
        return json.loads(line)
    except ValueError:
        return None


def _bullets(description: str) -> list[str]:
    return [b.strip() for b in re.split(r"[••]", description or "") if b.strip()]


def _extract_requirements(prompt: str) -> dict:
    jd = _between(prompt, "Job Description:\n", "\n\nReturn this exact JSON")
    terms = list(dict.fromkeys(_TECH_TERM.findall(jd)))
    return {
        "role_summary": "Builds and ships the systems this posting describes.",
        "must_have_skills": terms[:3],
        "nice_to_have_skills": terms[3:5],
        "key_responsibilities": ["Build production systems",
                                 "Work with the team on delivery",
                                 "Measure and improve quality"],
        "keywords_to_include": terms[:5],
    }


def _tailor_bullets(prompt: str) -> dict:
    original = _between(prompt, "Original project bullets:\n",
                        "\n\nJD keywords to mirror")
    return {"tailored_bullets": _bullets(original)}


def _sorted_skills(prompt: str) -> list[str]:
    skills = _json_after(prompt, "Candidate skills: ")
    return sorted(skills or [], key=str.lower)


def _order_skills(prompt: str) -> dict:
    return {"ordered_skills": _sorted_skills(prompt)}


def _tailor_resume(prompt: str) -> dict:
    projects = json.loads(_between(prompt, "Projects (JSON):\n", "\n\n") or "[]")
    return {"projects": [{"title": p.get("title", ""),
                          "tailored_bullets": _bullets(p.get("bullets", ""))}
                         for p in projects],
            "ordered_skills": _sorted_skills(prompt)}


def _cover_letter(prompt: str) -> dict:
    company = _between(prompt, "Target role: ", "\n").rsplit(" at ", 1)[-1].strip()
    project = _between(prompt, "(the ONLY permitted evidence):\n- ", ":").strip()
    return {"paragraphs": [
        f"The candidate is applying to {company} with project evidence.",
        f"The strongest evidence is the {project} project." if project
        else "The strongest evidence is the project work.",
        "The candidate is available immediately.",
    ]}


ANSWERS: dict[str, Callable[[str], dict]] = {
    "extract_requirements": _extract_requirements,
    "tailor_bullets": _tailor_bullets,
    "order_skills": _order_skills,
    "tailor_resume": _tailor_resume,
    "cover_letter": _cover_letter,
}


# ---- controls -----------------------------------------------------------------
def _env_map(name: str) -> dict[str, str]:
    out = {}
    for item in (os.environ.get(name) or "").split(","):
        step, _, value = item.partition("=")
        if step.strip() and value.strip():
            out[step.strip()] = value.strip()
    return out


class FakeControl:
    """In-process test hook: per-step call counts and answer overrides."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: Counter = Counter()
        self._overrides: dict[str, Callable[[str, Any], Any]] = {}

    def on(self, step: str, fn: Callable[[str, Any], Any]) -> None:
        if step not in ANSWERS:
            raise ValueError(f"unknown fake LLM step {step!r}")
        self._overrides[step] = fn

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
            self._overrides.clear()

    def _record(self, step: str) -> int:
        with self._lock:
            self.calls[step] += 1
            return self.calls[step]


CONTROL = FakeControl()


def _log_call(step: str, n: int) -> None:
    path = os.environ.get("FAKE_LLM_LOG")
    if not path:
        return
    line = json.dumps({"step": step, "pid": os.getpid(), "n": n}) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_call_log(path: str | Path) -> Counter:
    """Calls per step across every process that logged to `path`."""
    p = Path(path)
    if not p.exists():
        return Counter()
    return Counter(json.loads(line)["step"]
                   for line in p.read_text(encoding="utf-8").splitlines()
                   if line.strip())


class FakeProvider:
    name = "fake"

    def generate(self, prompt: str, model: str | None = None):
        step = classify(prompt)
        n = CONTROL._record(step)
        _log_call(step, n)
        sleep_s = _env_map("FAKE_LLM_SLEEP").get(step)
        if sleep_s:
            time.sleep(float(sleep_s))
        mode = _env_map("FAKE_LLM_RAISE").get(step)
        if mode == "unavailable":
            raise LLMUnavailableError(
                f"fake LLM provider: {step} unavailable (FAKE_LLM_RAISE)")
        if mode == "error":
            raise RuntimeError(f"fake LLM provider: {step} failed (FAKE_LLM_RAISE)")
        if mode == "none":
            return None
        answer = ANSWERS[step](prompt)
        override = CONTROL._overrides.get(step)
        return override(prompt, answer) if override else answer
