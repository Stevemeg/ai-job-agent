"""
backend.analysis -- UI-agnostic career intelligence layer.

Every module here is a pure function over candidate_profile.json and/or
jobs.json. No Streamlit, no Ollama, no network, no side effects. The
Streamlit UI renders these results today; a FastAPI backend can expose
them as endpoints tomorrow without any rewrite.
"""
