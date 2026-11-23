"""
backend.llm -- provider abstraction for LLM text generation.

Engines call generate(prompt) and never know which backend serves it.
LLM_PROVIDER picks one (default "auto"):

    auto, GROQ_API_KEY present (Streamlit secrets or env) ->  Groq   (deployment)
    auto, otherwise                                       ->  Ollama (local dev)
    groq / ollama                                         ->  that one
    fake                 ->  deterministic, offline (CI and tests; fake_provider.py)

Every provider honors one contract: return a parsed JSON dict (the prompts
all demand JSON), return None for unparseable output, raise
LLMUnavailableError when the backend cannot be reached/authenticated.
The deterministic hallucination validator downstream is provider-agnostic
by design -- it checks OUTPUT, never trusting any provider.
"""
from .provider import (generate, get_provider, reset_provider,  # noqa: F401
                       LLMUnavailableError)
