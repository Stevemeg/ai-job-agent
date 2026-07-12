"""Shared fixtures. Tests use synthetic fixtures (not the developer's real
profile) so they pass on any machine, including CI with no data/ directory."""
import sys
from pathlib import Path

import pytest

# Make `backend.*` importable regardless of how pytest is invoked.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
