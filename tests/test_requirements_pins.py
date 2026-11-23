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
