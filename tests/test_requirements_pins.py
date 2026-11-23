"""The agent's dependency pins (ADR-001: pin the fast-moving LangGraph
stack): one block, identical in requirements.txt and the Docker image's
requirements-docker.txt, every entry an exact pin, and LangGraph's own
transitive dependencies included."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEGIN, END = "# agent-pins:begin", "# agent-pins:end"


def _block(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    start, end = text.index(BEGIN), text.index(END)
    lines = text[start:end].splitlines()[1:]
    return [line.strip() for line in lines
            if line.strip() and not line.strip().startswith("#")]


def test_the_block_is_identical_in_both_files():
    assert _block("requirements.txt") == _block("requirements-docker.txt")


def test_every_entry_is_an_exact_pin():
    for line in _block("requirements.txt"):
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+(\[[a-z,]+\])?==[0-9][0-9A-Za-z.]*", line), line


def test_langgraphs_transitive_packages_are_pinned():
    names = {re.split(r"[\[=]", line)[0].lower() for line in _block("requirements.txt")}
    assert {"langgraph", "langgraph-checkpoint", "langgraph-checkpoint-postgres",
            "langchain-core", "langgraph-prebuilt", "langgraph-sdk", "langsmith",
            "ormsgpack", "orjson", "xxhash"} <= names


def test_nothing_else_names_a_langgraph_package_outside_the_block():
    for name in ("requirements.txt", "requirements-docker.txt"):
        text = (ROOT / name).read_text(encoding="utf-8")
        outside = text[:text.index(BEGIN)] + text[text.index(END):]
        assert not re.search(r"^(langgraph|langchain|langsmith)", outside, re.M), name


# ---- the API framework (Phase 6): FastAPI + Starlette pinned everywhere -------

API_PINS = ("fastapi", "starlette")


def _pins(text):
    return {m.group(1).lower(): m.group(2) for m in re.finditer(
        r"(?m)^\s*\"?([A-Za-z][A-Za-z0-9_.\-]*)==([0-9][0-9A-Za-z.]*)\"?", text)}


def _ci_install_pins():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    [line] = [ln for ln in ci.splitlines() if "pip install" in ln and "fastapi" in ln]
    return dict(re.findall(r"\"?([A-Za-z][A-Za-z0-9_.\-]*)==([0-9][0-9A-Za-z.]*)\"?", line))


def test_fastapi_and_starlette_are_pinned_identically_in_both_files_and_ci():
    reqs = _pins((ROOT / "requirements.txt").read_text(encoding="utf-8"))
    docker = _pins((ROOT / "requirements-docker.txt").read_text(encoding="utf-8"))
    ci = {k.lower(): v for k, v in _ci_install_pins().items()}
    for name in API_PINS:
        assert name in reqs, f"{name} is not pinned exactly in requirements.txt"
        assert docker.get(name) == reqs[name], name
        assert ci.get(name) == reqs[name], name
    for name in ("requirements.txt", "requirements-docker.txt"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"(?m)^fastapi\s*>=", text), name
