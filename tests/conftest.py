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


# ---- ranking -----------------------------------------------------------------

def fake_semantic_score(candidate_text, job_text):
    """Deterministic stand-in for the embedding similarity (CI has no
    torch): a stable function of both texts, in [0, 1). Everything else in
    the matcher -- gates, skill/role/seniority scores, explainer -- is real."""
    import hashlib
    digest = hashlib.sha256(f"{candidate_text}\x00{job_text}".encode()).digest()
    return int.from_bytes(digest[:4], "big") / 2 ** 32


@pytest.fixture
def stub_semantic(monkeypatch):
    from backend.matching_engine import scorer
    monkeypatch.setattr(scorer, "semantic_score", fake_semantic_score)
    return fake_semantic_score


@pytest.fixture
def rank_files(tmp_path, monkeypatch, profile):
    """(profile_file, ranked_file) under tmp_path, patched into
    services.ranking (the profile is the `profile` fixture's)."""
    import json
    from backend.services import ranking
    profile_file = tmp_path / "candidate_profile.json"
    profile_file.write_text(json.dumps(profile), encoding="utf-8")
    ranked_file = tmp_path / "ranked_jobs.json"
    monkeypatch.setattr(ranking, "PROFILE_FILE", profile_file)
    monkeypatch.setattr(ranking, "RANKED_FILE", ranked_file)
    return profile_file, ranked_file


# ---- LLM ---------------------------------------------------------------------

@pytest.fixture
def fake_llm(monkeypatch):
    """LLM_PROVIDER=fake for this test, with fresh call counts and no
    overrides (backend/llm/fake_provider.py). Yields its CONTROL hook."""
    from backend.llm import fake_provider, provider
    for var in ("FAKE_LLM_SLEEP", "FAKE_LLM_RAISE", "FAKE_LLM_LOG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    provider.reset_provider()
    fake_provider.CONTROL.reset()
    yield fake_provider.CONTROL
    fake_provider.CONTROL.reset()
    provider.reset_provider()


@pytest.fixture
def agent_db(pg, monkeypatch):
    """`pg`, with LangGraph's checkpointer (psycopg 3, its own connection)
    pointed at the same throwaway schema: libpq reads PGOPTIONS for both
    drivers. The checkpointer's once-per-process setup() is re-armed."""
    from sqlalchemy import text
    from backend.agent import checkpoint
    with pg.connect() as conn:
        schema = conn.execute(text("SELECT current_schema()")).scalar()
    monkeypatch.setenv("PGOPTIONS", f"-csearch_path={schema}")
    checkpoint.reset()
    yield pg
    checkpoint.reset()


AGENT_ENV_VARS = ("AGENT_ENABLED", "AGENT_MIN_SCORE", "AGENT_MAX_JOB_AGE_DAYS",
                  "AGENT_MAX_DRAFTS_PER_RUN", "AGENT_LOCATIONS",
                  "AGENT_ACCEPT_GLOBAL_REMOTE", "AGENT_APPROVAL_TTL_DAYS",
                  "AGENT_EXCLUDE_TITLE_WORDS", "AGENT_MEMORY_MAX_BOOST",
                  "AGENT_LEARN_COMPANY_DECLINES", "AGENT_LEARN_TITLE_DECLINES",
                  "AGENT_LEARN_PREFER_APPROVALS", "AGENT_FOLLOW_UP_DAYS")


@pytest.fixture
def agent_env(agent_db, fake_llm, rank_files, stub_semantic, tmp_path, monkeypatch):
    """Everything the agent graphs touch, isolated: the throwaway schema
    (with the checkpointer), the fake LLM, the profile and ranked file under
    tmp_path, the embedding similarity stubbed, data/applications/ under
    tmp_path, and the AGENT_* settings at their defaults."""
    from types import SimpleNamespace
    from backend.agent import application_graph
    apps = tmp_path / "applications"
    monkeypatch.setattr(application_graph, "APPLICATIONS_DIR", apps)
    for var in AGENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return SimpleNamespace(pg=agent_db, llm=fake_llm, apps=apps,
                           profile_file=rank_files[0], ranked_file=rank_files[1])
