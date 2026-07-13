"""
app.py -- Universal AI Job Agent: entry point + router.

Upload-first flow (session-state stage machine, not Streamlit multipage,
because multipage lists every page in the sidebar and would break the
"nothing until upload" requirement):

    landing  -> upload + parse resume -> dashboard
    dashboard -> tabs: Overview | Skills | Skill Gaps | Suggestions |
                       Career Paths | Jobs

This file only routes and holds layout chrome. Analysis math lives in
backend/analysis; rendering lives in backend/ui/views; styling in theme.py.

Run from project root:
    streamlit run backend/ui/app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import streamlit as st

# Streamlit executes this file as a plain script (no package context), so
# put the project root on sys.path once, then use absolute imports.
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.config import PROFILE_FILE, JOBS_FILE, SAMPLE_JOBS_FILE  # noqa: E402
from backend.ui import theme                                      # noqa: E402
from backend.ui.views import landing, review, analysis, jobs, tracker  # noqa: E402

st.set_page_config(page_title="Universal AI Job Agent", page_icon="🎯",
                   layout="wide")
theme.inject_theme()

if "stage" not in st.session_state:
    st.session_state.stage = "landing"


def _load_profile() -> dict | None:
    if not PROFILE_FILE.exists():
        return None
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _active_jobs_file() -> Path | None:
    # Full corpus (JOBS_FILE) is gitignored -- local/self-hosted only.
    # Deployed instances fall back to the small committed sample corpus.
    if JOBS_FILE.exists():
        return JOBS_FILE
    if SAMPLE_JOBS_FILE.exists():
        return SAMPLE_JOBS_FILE
    return None


def _load_jobs() -> list[dict]:
    jobs_file = _active_jobs_file()
    if jobs_file is None:
        return []
    with open(jobs_file, "r", encoding="utf-8") as f:
        return json.load(f)


# ---- routing ----------------------------------------------------------------

if st.session_state.stage == "landing":
    landing.render()
    st.stop()

profile = _load_profile()
if profile is None:                      # later stage requested but no profile
    st.session_state.stage = "landing"
    st.rerun()

if st.session_state.stage == "review":
    review.render(profile)
    st.stop()

# ---- sidebar ----------------------------------------------------------------

with st.sidebar:
    st.markdown(f"### {profile.get('name', 'Candidate')}")
    st.markdown(f'<span class="uja-muted">{profile.get("email", "")}</span>',
                unsafe_allow_html=True)
    st.divider()
    if st.button("✏️ Edit extracted profile", use_container_width=True):
        st.session_state.stage = "review"
        st.rerun()
    if st.button("← Start over", use_container_width=True):
        st.session_state.stage = "landing"
        st.rerun()
    st.divider()
    from backend.version import __version__
    st.caption(f"Universal AI Job Agent v{__version__}")

# ---- dashboard ---------------------------------------------------------------

all_jobs = _load_jobs()
_active_file = _active_jobs_file()
jobs_mtime = _active_file.stat().st_mtime if _active_file else 0.0
results = analysis.cached_analysis(profile, jobs_mtime, all_jobs)

analysis.render_hero(profile, results)

(tab_overview, tab_skills, tab_gaps, tab_suggest, tab_careers, tab_jobs,
 tab_tracker) = st.tabs(
    ["Overview", "Skills", "Skill Gaps", "Suggestions", "Career Paths",
     f"Jobs ({len(all_jobs):,})" if all_jobs else "Jobs", "Tracker"])

with tab_overview:
    analysis.render_overview(profile, results["health"])
with tab_skills:
    analysis.render_skills(profile)
with tab_gaps:
    analysis.render_gaps(results["gaps"], results["careers"])
with tab_suggest:
    analysis.render_suggestions(results["suggestions"])
with tab_careers:
    analysis.render_careers(results["careers"])
with tab_jobs:
    jobs.render(profile)
with tab_tracker:
    tracker.render(profile)
