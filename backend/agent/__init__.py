"""
backend.agent -- the Phase 4 agent graphs (ADR-001 §1), DRY RUN: they
draft documents and record them; nothing leaves the system.

Two LangGraph graphs, each checkpointed in Postgres (checkpoint.py) and run
by the worker as a queue task:

  "agent_run"   (run_graph.py, thread "agent_run:<task_id>")
      refresh_ranking -> triage -> dispatch
      Re-ranks incrementally, picks the best new jobs for the current
      profile, and creates one applications row + one "application" task
      per job, each pair in one transaction.

  "application" (application_graph.py, thread "application:<id>")
      load -> extract_requirements -> tailor_resume -> validate_resume ->
      cover_letter -> validate_letter -> render -> persist
      One LLM call per LLM node. A retried task resumes from its last
      checkpoint instead of starting over.

Importing this package is cheap: LangGraph loads only when a graph runs.
"""
