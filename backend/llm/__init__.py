"""
backend.llm -- provider abstraction for LLM text generation.

Engines call generate(prompt) and never know which backend serves it:

    GROQ_API_KEY present (Streamlit secrets or env)  ->  Groq   (deployment)
    otherwise                                        ->  Ollama (local dev)

Both providers honor one contract: return a parsed JSON dict (the prompts
all demand JSON), return None for unparseable output, raise
LLMUnavailableError when the backend cannot be reached/authenticated.
The deterministic hallucination validator downstream is provider-agnostic
by design -- it checks OUTPUT, never trusting any provider.
"""
from .provider import generate, get_provider, LLMUnavailableError  # noqa: F401
