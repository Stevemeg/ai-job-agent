"""Ollama provider -- local inference for development.

Logic moved verbatim from resume_tailor.call_ollama (same URL, same
timeout behavior, same error messages) so local behavior is unchanged.
"""
from __future__ import annotations

import os

import requests

from .provider import LLMUnavailableError, parse_json_output

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_OLLAMA_MODEL = "mistral"
# First call after `ollama serve` also LOADS the ~4.4 GB model into memory,
# which alone can take a minute+ on modest hardware.
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "480"))


class OllamaProvider:
    name = "ollama"

    def generate(self, prompt: str, model: str | None = None):
        payload = {
            "model": model or DEFAULT_OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.3},
        }
        try:
            resp = requests.post(OLLAMA_URL, json=payload,
                                 timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status()
        except requests.exceptions.ConnectionError:
            raise LLMUnavailableError(
                "No LLM backend available: Ollama is not running locally "
                "(start it with `ollama serve`), and no GROQ_API_KEY is "
                "configured (add one to Streamlit Secrets or the "
                "environment to use hosted inference).")
        except requests.exceptions.Timeout:
            raise RuntimeError(
                f"The model didn't respond within {OLLAMA_TIMEOUT}s. The "
                "first call after `ollama serve` also loads the model "
                "(~4.4 GB) into memory, which is slow on modest hardware. "
                "Try again (the model is likely warm now), or raise the "
                "OLLAMA_TIMEOUT environment variable.")
        return parse_json_output(resp.json().get("response", ""))
