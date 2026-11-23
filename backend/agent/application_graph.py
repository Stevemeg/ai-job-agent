"""
application_graph.py -- draft one application (Phase 4, DRY RUN): one
LangGraph thread "application:<application_id>", run by the worker for an
"application" task.

  load -> extract_requirements -> tailor_resume -> validate_resume
       -> cover_letter -> validate_letter -> render -> persist -> END

- One LLM call per LLM node (extract_requirements, tailor_resume,
  cover_letter); every other node makes none. The JD requirements are
  extracted once and shared through the graph state by both documents.
- The validators are the resume engine's, unchanged: a flagged resume
  project falls back to its original bullets, a flagged letter paragraph is
  reported. Nothing is regenerated. Any flag sets needs_review.
- render writes data/applications/<application_id>/ (resume.docx,
  cover_letter.docx, cover_letter.txt, jd_requirements.json) -- never the
  shared DATA_DIR names the UI and CLIs use.
- persist upserts the artifacts by (application_id, kind), moves the
  application to awaiting_approval (version 1, needs_review = any flag),
  queues its approval expiry and logs ONE 'drafted' event, in one
  transaction (idempotent).
- The handler always invokes the graph on the same thread: a retried task
  resumes at the node the last attempt died in (checkpoint.run_thread).
  An LLM outage (LLMUnavailableError) fails the attempt, so it is retried
  with backoff; when the task is dead, on_application_dead() marks the
  application 'failed' with the error.

No outbound actions: the only network traffic is the LLM provider's.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, TypedDict

from ..config import DATA_DIR
from . import store

log = logging.getLogger(__name__)

APPLICATIONS_DIR = DATA_DIR / "applications"
ARTIFACT_FILES = {"resume_docx": "resume.docx",
                  "cover_letter_docx": "cover_letter.docx",
                  "cover_letter_txt": "cover_letter.txt",
                  "jd_requirements": "jd_requirements.json"}
# The provider's default model (Groq: llama-3.3-70b-versatile, Ollama:
# mistral), as for the UI and CLIs.
MODEL = None


class AppState(TypedDict, total=False):
    application_id: int
    user_id: int
    job_id: int
    job: dict
    profile: dict
    jd_requirements: Any
    tailored: dict
    resume_content: dict
    resume_report: list
    paragraphs: list
    letter_review: list
    artifacts: dict
    status: str
    needs_review: bool
    version: int


def application_dir(application_id: int) -> Path:
    return APPLICATIONS_DIR / str(application_id)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def needs_review(resume_report: list, letter_review: list) -> bool:
    return any(not r["ok"] for r in resume_report) or bool(letter_review)


def build_application_graph(ctx: Any = None):
    """The application graph's StateGraph builder; `ctx` is the worker's
    TaskContext (lease checks before writes), or None."""
    from langgraph.graph import END, START, StateGraph
    from ..resume_engine import cover_letter, resume_tailor
    from ..services import ranking

    def lease() -> None:
        if ctx is not None:
            ctx.ensure_lease()

    def load(state: AppState) -> dict:
        app = store.load_application(state["application_id"])
        profile = ranking._load_profile()
        lease()
        store.set_status(app["id"], "drafting", only_if_not_done=True)
        job = {k: app[k] for k in ("title", "company", "location", "apply_link",
                                   "clean_description", "tags", "seniority")}
        return {"user_id": app["user_id"], "job_id": app["job_id"], "job": job,
                "profile": profile}

    def extract_requirements(state: AppState) -> dict:
        jd = state["job"].get("clean_description") or ""
        return {"jd_requirements": resume_tailor.extract_jd_requirements(jd, MODEL)}

    def tailor_resume(state: AppState) -> dict:
        return {"tailored": resume_tailor.tailor_resume_single_call(
            state["profile"], state["jd_requirements"], MODEL)}

    def validate_resume(state: AppState) -> dict:
        content, report = resume_tailor.build_tailored_resume(
            state["profile"], state["job"], state["jd_requirements"], MODEL,
            tailored=state["tailored"], verbose=False)
        return {"resume_content": content, "resume_report": report}

    def write_cover_letter(state: AppState) -> dict:
        paragraphs = cover_letter.draft_cover_letter(
            state["profile"], state["job"], state["jd_requirements"], MODEL)
        if not paragraphs:
            raise RuntimeError("The model did not return usable cover-letter "
                               "paragraphs (LLM output is non-deterministic; "
                               "the task retries this node).")
        return {"paragraphs": paragraphs}

    def validate_letter(state: AppState) -> dict:
        return {"letter_review": cover_letter.letter_review(
            state["paragraphs"], state["profile"], state["job"])}

    def render(state: AppState) -> dict:
        lease()
        out = application_dir(state["application_id"])
        out.mkdir(parents=True, exist_ok=True)
        paths = {kind: out / name for kind, name in ARTIFACT_FILES.items()}
        paths["jd_requirements"].write_text(
            json.dumps(state["jd_requirements"], indent=2, ensure_ascii=False),
            encoding="utf-8")
        resume_tailor.render_resume_docx(state["resume_content"], paths["resume_docx"])
        try:
            cover_letter.render_cover_letter(state["paragraphs"],
                                             paths["cover_letter_docx"],
                                             state["profile"], state["job"])
        except FileNotFoundError:
            raise RuntimeError("Node.js not found -- .docx generation needs node "
                               "on PATH (the Docker image has it).") from None
        cover_letter.render_cover_letter(state["paragraphs"], paths["cover_letter_txt"],
                                         state["profile"], state["job"])
        return {"artifacts": {kind: {"path": str(p), "sha256": _sha256(p)}
                              for kind, p in paths.items()}}

    def persist(state: AppState) -> dict:
        resume_report, review = state["resume_report"], state["letter_review"]
        flagged = needs_review(resume_report, review)
        version = 1
        resume = {"ok": all(r["ok"] for r in resume_report), "projects": resume_report}
        letter = {"ok": not review, "paragraphs": review}
        reports = {"resume_docx": resume, "cover_letter_docx": letter,
                   "cover_letter_txt": letter, "jd_requirements": None}
        artifacts = {kind: {**a, "report": reports[kind]}
                     for kind, a in state["artifacts"].items()}
        lease()
        logged = store.persist(state["application_id"], artifacts,
                               needs_review=flagged, version=version)
        log.info("application %s: awaiting approval at version %d%s (%s)",
                 state["application_id"], version,
                 ", needs review" if flagged else "",
                 "drafted event logged" if logged else "already recorded")
        return {"status": "awaiting_approval", "needs_review": flagged,
                "version": version}

    g = StateGraph(AppState)
    nodes = [("load", load), ("extract_requirements", extract_requirements),
             ("tailor_resume", tailor_resume), ("validate_resume", validate_resume),
             ("cover_letter", write_cover_letter), ("validate_letter", validate_letter),
             ("render", render), ("persist", persist)]
    for name, fn in nodes:
        g.add_node(name, fn)
    g.add_edge(START, nodes[0][0])
    for (a, _), (b, _) in zip(nodes, nodes[1:]):
        g.add_edge(a, b)
    g.add_edge(nodes[-1][0], END)
    return g


NODES = ("load", "extract_requirements", "tailor_resume", "validate_resume",
         "cover_letter", "validate_letter", "render", "persist")


def run_application_task(payload: dict, ctx: Any = None) -> dict:
    """Worker handler for "application" tasks."""
    from .checkpoint import run_thread
    application_id = int(payload["application_id"])
    final = run_thread(build_application_graph(ctx),
                       store.application_thread(application_id),
                       {"application_id": application_id})
    return {"application_id": application_id, "status": final["status"],
            "needs_review": final["needs_review"], "version": final["version"],
            "artifacts": {k: a["sha256"] for k, a in final["artifacts"].items()},
            "resume_flags": sum(not r["ok"] for r in final["resume_report"]),
            "letter_flags": len(final["letter_review"])}


def on_application_dead(payload: dict, error: str | None) -> None:
    """Dead-task hook (the worker calls it once the task has no attempts
    left): the application is 'failed', with the last error's summary. A
    finished draft (APPLICATION_DONE_STATUSES) is never overwritten."""
    from ..services.task_status import error_summary
    application_id = int(payload["application_id"])
    changed = store.set_status(application_id, "failed",
                               error=error_summary(error) or "task died",
                               only_if_not_done=True)
    if changed:
        log.error("application %s failed: %s", application_id, error_summary(error))
