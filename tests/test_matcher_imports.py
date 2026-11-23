"""The matcher imports without the ML stack: only semantic_score() needs
sklearn and sentence-transformers (torch), and it loads them on first use.
That is what lets CI run the real gates, sub-scores and explainer (with the
embedding stubbed) -- e.g. the incremental-ranking parity test."""
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_PROBE = textwrap.dedent("""
    import sys
    BLOCKED = ("sentence_transformers", "sklearn", "torch", "transformers")

    class Block:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError("blocked: " + name)

    sys.meta_path.insert(0, Block())
    from backend.matching_engine import matcher, scorer

    job = {"title": "AI Engineer", "company": "acme", "tags": ["ml"],
           "seniority": "Junior", "clean_description": "Build RAG with FAISS."}
    profile = {"skills": [{"skill": "RAG"}, {"skill": "Rust"}]}
    assert matcher.passes_hard_filters(job, True)
    assert matcher.explain_match(profile, job)["strong_matches"] == ["RAG"]
    assert scorer.seniority_score(job, True) == 1.0
    try:
        scorer.semantic_score("a", "b")
    except ImportError:
        pass
    else:
        raise AssertionError("semantic_score ran without the ML stack?")
    loaded = sorted(m for m in sys.modules if m.split(".")[0] in BLOCKED)
    assert loaded == [], loaded
    print("ok")
""")


def test_matcher_imports_and_gates_without_the_ml_stack():
    proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"
