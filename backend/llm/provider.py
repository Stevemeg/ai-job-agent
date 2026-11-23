"""Provider selection + shared contract utilities."""
from __future__ import annotations

import json
import os
import re


class LLMUnavailableError(RuntimeError):
    """The LLM backend can't be reached or authenticated. Carries a
    user-facing message explaining exactly how to fix it (start Ollama /
    set GROQ_API_KEY), so UIs can display str(exc) directly."""


def parse_json_output(raw: str):
    """Normalize model output to a dict: strip markdown fences, parse JSON.
    Returns None (never raises) on unparseable output -- every engine
    call site already has a truthful fallback for None."""
    raw = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(),
                 flags=re.MULTILINE)
    raw = re.sub(r"\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        return json.loads(raw.strip())
    except json.JSONDecodeError:
        print(f"WARNING: model returned non-JSON output: {raw[:200]}")
        return None


def get_groq_api_key() -> str | None:
    """Streamlit Secrets first (deployment), then environment (CLI/API/
    Docker). Never hardcoded, never logged."""
    try:
        import streamlit as st
        try:
            if "GROQ_API_KEY" in st.secrets:
                return st.secrets["GROQ_API_KEY"]
        except Exception:
            pass          # no secrets.toml at all -> fall through to env
    except ImportError:
        pass              # non-Streamlit context (CLI, API, tests)
    return os.environ.get("GROQ_API_KEY")


_provider = None

PROVIDER_CHOICES = ("auto", "groq", "ollama", "fake")


def provider_choice() -> str:
    """LLM_PROVIDER (default "auto"), validated."""
    choice = (os.environ.get("LLM_PROVIDER") or "auto").strip().lower()
    if choice not in PROVIDER_CHOICES:
        raise ValueError(f"LLM_PROVIDER must be one of {', '.join(PROVIDER_CHOICES)}; "
                         f"got {choice!r}")
    return choice


def get_provider():
    """The provider LLM_PROVIDER names. "auto" (the default, and v1's only
    behaviour): Groq when a key exists, Ollama otherwise. "fake": the
    deterministic offline provider for CI and tests (fake_provider.py).
    Cached per process (reset_provider() drops the cache)."""
    global _provider
    if _provider is None:
        choice = provider_choice()
        if choice == "fake":
            from .fake_provider import FakeProvider
            _provider = FakeProvider()
            return _provider
        key = get_groq_api_key() if choice in ("auto", "groq") else None
        if key:
            from .groq_provider import GroqProvider
            _provider = GroqProvider(key)
        elif choice == "groq":
            raise LLMUnavailableError(
                "LLM_PROVIDER=groq but no GROQ_API_KEY is configured (add one "
                "to Streamlit Secrets or the environment).")
        else:
            from .ollama_provider import OllamaProvider
            _provider = OllamaProvider()
    return _provider


def reset_provider() -> None:
    """Forget the cached provider (the next generate() re-reads LLM_PROVIDER)."""
    global _provider
    _provider = None


def generate(prompt: str, model: str | None = None):
    """The only function engines should call.

    model=None means "the provider's default" -- model IDs are
    provider-specific, so callers should pass one only deliberately."""
    return get_provider().generate(prompt, model=model)
