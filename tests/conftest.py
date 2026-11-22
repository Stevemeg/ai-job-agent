"""Shared fixtures. Tests use synthetic fixtures (not the developer's real
profile) so they pass on any machine, including CI with no data/ directory.

Database isolation:
- Tests marked ``db`` get a throwaway Postgres schema (``pg`` fixture) that
  is dropped afterwards, so they can never read or modify a developer's
  real tracker/corpus data even when pointed at the local dev database.
- Every other test runs with DATABASE_URL/DB_HOST removed from the
  environment, so the DB-first corpus loader always takes the JSON path
  there, exactly as it did before the corpus moved to Postgres.
"""
import os
import sys
import uuid
from pathlib import Path

import pytest

# Make `backend.*` importable regardless of how pytest is invoked.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB_ENV_VARS = ("DATABASE_URL", "DB_HOST")


def _db_configured() -> bool:
    return any(os.environ.get(v) for v in DB_ENV_VARS)


def _reset_corpus_caches() -> None:
    # Only if something already imported it -- keeps conftest dependency-free.
    corpus = sys.modules.get("backend.database.corpus")
    if corpus is not None:
        corpus._reset_caches()


@pytest.fixture(autouse=True)
def _db_isolation(request, monkeypatch):
    if request.node.get_closest_marker("db"):
        if not _db_configured():
            if os.environ.get("CI"):
                pytest.fail("DB tests must run in CI, but neither DATABASE_URL "
                            "nor DB_HOST is set -- check the workflow's "
                            "postgres service env.")
            pytest.skip("no database configured (set DB_HOST or DATABASE_URL)")
    else:
        for var in DB_ENV_VARS:
            monkeypatch.delenv(var, raising=False)
    _reset_corpus_caches()
    yield
    _reset_corpus_caches()


@pytest.fixture
def pg(monkeypatch):
    """A Postgres engine bound (via search_path) to a fresh, empty schema.

    backend.database.db's engine/session/init memo are pointed at it for the
    duration of the test, so production code paths (init_db, get_session,
    corpus functions) run unmodified against the throwaway schema.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool
    from backend.database import db

    url = db._build_db_url()
    schema = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(url, poolclass=NullPool,
                          connect_args={"connect_timeout": 5})
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, poolclass=NullPool, connect_args={
        "connect_timeout": 5, "options": f"-csearch_path={schema}"})

    monkeypatch.setattr(db, "_engine", engine)
    monkeypatch.setattr(db, "_SessionLocal", None)
    monkeypatch.setattr(db, "_initialized", False)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def profile():
    """A realistic, fully-populated candidate profile."""
    return {
        "name": "Test Candidate",
        "email": "test@example.com",
        "phone": "+91 9999999999",
        "linkedin": "https://linkedin.com/in/test",
        "github": "https://github.com/test",
        "skills": [
            {"skill": "RAG", "category": "Generative AI", "domain": "AI"},
            {"skill": "LLM", "category": "Generative AI", "domain": "AI"},
            {"skill": "FAISS", "category": "Vector Database", "domain": "AI"},
            {"skill": "Ollama", "category": "LLM Platform", "domain": "AI"},
            {"skill": "OpenCV", "category": "Computer Vision", "domain": "AI"},
            {"skill": "CLIP", "category": "Multimodal AI", "domain": "AI"},
            {"skill": "Pandas", "category": "Data Science", "domain": "AI"},
            {"skill": "NumPy", "category": "Data Science", "domain": "AI"},
            {"skill": "Flask", "category": "Backend Framework", "domain": "Software"},
            {"skill": "Git", "category": "Version Control", "domain": "Software"},
            {"skill": "Sentence Transformers", "category": "NLP Framework", "domain": "AI"},
            {"skill": "GANs", "category": "Generative AI", "domain": "AI"},
        ],
        "education": [{"college": "Test Institute", "degree": "B.E. AI/ML",
                       "cgpa": "8.0", "years": "2022 - 2026"}],
        "projects": [
            {
                "title": "RAG Medical Copilot",
                "duration": "Oct 2025 – Nov 2025",
                "description": (" • Designed and deployed a Retrieval-Augmented "
                                "Generation pipeline for clinical Q&A. "
                                "• Implemented semantic search using Sentence "
                                "Transformers and FAISS, indexing 2,500+ chunks "
                                "with sub-second latency. "
                                "• Integrated a local LLM via Ollama for offline "
                                "inference."),
            },
            {
                "title": "GAN Image Synthesizer",
                "duration": "Mar 2025 – Aug 2025",
                "description": (" • Engineered GAN pipelines generating 10,000+ "
                                "synthetic images, expanding datasets by 3x. "
                                "• Evaluated realism using FID and SSIM metrics. "
                                "• Built training loop with checkpointing."),
            },
        ],
    }


@pytest.fixture
def jobs():
    """Small synthetic corpus with known skill mentions."""
    def job(title, desc):
        return {"title": title, "company": "TestCo", "location": "Remote",
                "clean_description": desc, "tags": []}
    corpus = []
    # 40 AI-engineer-titled jobs: 20 mention docker, 10 mention kubernetes
    for i in range(40):
        desc = "Build LLM systems."
        if i < 20:
            desc += " Experience with Docker required."
        if i < 10:
            desc += " Kubernetes a plus."
        corpus.append(job(f"AI Engineer {i}", desc))
    # 60 unrelated jobs, 6 mention docker
    for i in range(60):
        desc = "General software role." + (" Docker." if i < 6 else "")
        corpus.append(job(f"Accountant {i}", desc))
    return corpus
