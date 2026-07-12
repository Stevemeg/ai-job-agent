"""
backend.api -- the public HTTP interface.

Design rule: routers contain NO business logic. Every endpoint is a thin
adapter over backend.analysis / backend.matching_engine / backend.database /
backend.resume_parser -- the same pure functions the Streamlit UI renders.
If an endpoint needs new logic, the logic goes in the engine, not here.
"""
