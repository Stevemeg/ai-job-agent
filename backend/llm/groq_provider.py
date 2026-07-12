"""Groq provider -- hosted inference for deployment.

MODEL CHOICE: llama-3.3-70b-versatile.
- Listed in Groq's PRODUCTION tier (not preview -- preview models "may be
  discontinued at short notice" per Groq's docs), verified against
  https://console.groq.com/docs/models at implementation time.
- 70B-class instruction following: the tailoring prompts demand strict
  JSON and precise constraint-following ("do NOT add tools not in the
  original") -- the smaller llama-3.1-8b-instant is cheaper but measurably
  looser on constraints, and the validator would just reject more output.
- Supports response_format json_object, eliminating the markdown-fence
  failure mode entirely.
Override without code changes: GROQ_MODEL in Streamlit Secrets or env.
"""
from __future__ import annotations

import os

import requests

from .provider import LLMUnavailableError, parse_json_output

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"
GROQ_TIMEOUT = int(os.environ.get("GROQ_TIMEOUT", "120"))


def _configured_model() -> str:
    try:
        import streamlit as st
        try:
            if "GROQ_MODEL" in st.secrets:
                return st.secrets["GROQ_MODEL"]
        except Exception:
            pass
    except ImportError:
        pass
    return os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL)


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str):
        self._api_key = api_key

    def generate(self, prompt: str, model: str | None = None):
        payload = {
            "model": model or _configured_model(),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,                     # matches Ollama setting
            "response_format": {"type": "json_object"},
        }
        try:
            resp = requests.post(
                GROQ_URL, json=payload, timeout=GROQ_TIMEOUT,
                headers={"Authorization": f"Bearer {self._api_key}"})
        except requests.exceptions.ConnectionError:
            raise LLMUnavailableError(
                "Could not reach the Groq API -- check the network "
                "connection of the deployment.")
        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"Groq did not respond within {GROQ_TIMEOUT}s -- try again.")

        if resp.status_code == 401:
            raise LLMUnavailableError(
                "Groq rejected the API key (401). Add a valid GROQ_API_KEY "
                "in Streamlit Secrets (Manage app → Settings → Secrets).")
        if resp.status_code == 429:
            raise RuntimeError(
                "Groq rate limit reached -- wait a moment and try again.")
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Groq API error {resp.status_code}: {resp.text[:300]}")

        raw = resp.json()["choices"][0]["message"]["content"]
        return parse_json_output(raw)
