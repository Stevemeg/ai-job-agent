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


def get_provider():
    """Groq when a key exists, Ollama otherwise. Cached per process."""
    global _provider
    if _provider is None:
        key = get_groq_api_key()
        if key:
            from .groq_provider import GroqProvider
            _provider = GroqProvider(key)
        else:
            from .ollama_provider import OllamaProvider
            _provider = OllamaProvider()
    return _provider


def generate(prompt: str, model: str | None = None):
    """The only function engines should call.

    model=None means "the provider's default" -- model IDs are
    provider-specific, so callers should pass one only deliberately."""
    return get_provider().generate(prompt, model=model)
