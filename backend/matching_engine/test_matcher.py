"""Run from backend/:  python -m matching_engine.test_matcher
Old version had `from matcher import ...` which crashed (no package prefix)."""
from .scorer import score_job, passes_hard_filters

candidate_skills = ["Python", "PyTorch", "TensorFlow", "NLP", "RAG", "FAISS"]
candidate_text = ("Skills: Python PyTorch NLP RAG FAISS "
                  "Projects: Medical AI Copilot RAG pipeline with LLMs and FAISS")

good_job = {"title": "AI Engineer", "tags": ["Python", "LLM", "Machine Learning"],
            "seniority": "Junior", "experience": "1 years",
            "clean_description": "AI Engineer with NLP, Transformers, RAG, PyTorch."}
bad_job = {"title": "Enterprise Sales Engineer", "tags": ["sales", "travel"],
           "seniority": "Senior", "experience": "8 years",
           "clean_description": "Drive revenue, manage accounts."}

assert passes_hard_filters(good_job) is True
assert passes_hard_filters(bad_job) is False, "sales/8yr role should be gated out"

g_score, g_break = score_job(candidate_skills, candidate_text, good_job)
print("AI Engineer score:", g_score, g_break)
assert g_score > 40, "a strong on-track match should clear 40"
print("\nAll assertions passed.")