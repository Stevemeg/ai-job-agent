"""
career_recommender.py -- role-archetype fit scoring.

Scores the candidate against a set of role archetypes using evidence from
skills AND project text. Deliberately keyword-based, not embedding-based:
the output must be explainable ("why 91%? because RAG, FAISS, Ollama, and
your Copilot project"), and archetypes are data, so adding a new domain
(Mechanical Engineer, HR, Finance) means adding a dict entry -- no code.

Fit % = coverage of the archetype's core signals, weighted core vs bonus.
"""
from __future__ import annotations

import re
from typing import Any

# Each archetype: core signals (define the role) + bonus signals (strengthen).
ROLE_ARCHETYPES: dict[str, dict[str, list[str]]] = {
    "AI Engineer": {
        "core": ["llm", "rag", "prompt engineering", "ollama", "huggingface",
                 "transformers", "vector", "faiss", "semantic search"],
        "bonus": ["fine-tuning", "agents", "langchain", "embedding", "multimodal"],
    },
    "ML Engineer": {
        "core": ["pytorch", "tensorflow", "scikit-learn", "training pipeline",
                 "model", "deep learning", "cnn", "evaluation"],
        "bonus": ["mlops", "deployment", "docker", "checkpointing", "gan"],
    },
    "LLM / GenAI Engineer": {
        "core": ["llm", "rag", "prompt engineering", "generative", "gan",
                 "ollama", "fine-tuning"],
        "bonus": ["agents", "langchain", "vector", "faiss", "inference"],
    },
    "Computer Vision Engineer": {
        "core": ["opencv", "clip", "cnn", "image", "vision", "gan"],
        "bonus": ["multimodal", "segmentation", "detection", "medical imaging"],
    },
    "NLP Engineer": {
        "core": ["nlp", "transformers", "distilbert", "sentence transformers",
                 "semantic search", "tokeniz", "text"],
        "bonus": ["llm", "rag", "embedding", "faiss"],
    },
    "Data Scientist": {
        "core": ["pandas", "numpy", "scikit-learn", "analysis", "statistics",
                 "visualization", "evaluation"],
        "bonus": ["sql", "experiment", "a/b", "metrics", "modeling"],
    },
    "Backend Engineer": {
        "core": ["flask", "fastapi", "api", "postgres", "sql", "backend",
                 "database"],
        "bonus": ["docker", "redis", "microservice", "rest", "authentication"],
    },
    "Data Analyst": {
        "core": ["pandas", "numpy", "sql", "visualization", "analysis",
                 "dashboard", "excel"],
        "bonus": ["tableau", "power bi", "statistics", "reporting"],
    },
}

CORE_WEIGHT = 0.75
BONUS_WEIGHT = 0.25

# archetype -> job-title keywords, used by skill_gap.py to compute demand
# WITHIN the candidate's target roles (not just the whole corpus).
ROLE_TITLE_KEYWORDS: dict[str, list[str]] = {
    "AI Engineer": ["ai engineer", "artificial intelligence engineer"],
    "ML Engineer": ["ml engineer", "machine learning"],
    "LLM / GenAI Engineer": ["llm", "genai", "generative ai"],
    "Computer Vision Engineer": ["computer vision", "cv engineer", "perception"],
    "NLP Engineer": ["nlp", "natural language"],
    "Data Scientist": ["data scientist", "data science"],
    "Backend Engineer": ["backend", "back-end", "back end"],
    "Data Analyst": ["data analyst", "analytics"],
}


def _evidence_text(profile: dict) -> str:
    parts = [s["skill"] for s in profile.get("skills", [])]
    for p in profile.get("projects", []):
        parts += [p.get("title", ""), p.get("description", "")]
    return " ".join(parts).lower()


def _coverage(signals: list[str], text: str) -> tuple[float, list[str]]:
    hits = [s for s in signals
            if re.search(r"(?<![a-z])" + re.escape(s), text)]
    return (len(hits) / len(signals) if signals else 0.0), hits


def recommend_careers(profile: dict, top_n: int = 6) -> list[dict[str, Any]]:
    """Returns [{"role", "fit_pct", "evidence": [...]}] sorted by fit."""
    text = _evidence_text(profile)
    results = []
    for role, signals in ROLE_ARCHETYPES.items():
        core_cov, core_hits = _coverage(signals["core"], text)
        bonus_cov, bonus_hits = _coverage(signals["bonus"], text)
        fit = (core_cov * CORE_WEIGHT + bonus_cov * BONUS_WEIGHT) * 100
        if fit <= 0:
            continue
        results.append({
            "role": role,
            "fit_pct": round(fit),
            "evidence": core_hits + bonus_hits,
            # what would raise the fit -- core signals with no evidence
            "missing": [s for s in signals["core"] if s not in core_hits],
        })
    results.sort(key=lambda r: r["fit_pct"], reverse=True)
    return results[:top_n]
