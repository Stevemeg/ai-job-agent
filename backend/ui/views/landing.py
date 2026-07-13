"""
landing.py -- Step 1: upload-first landing page.

Nothing but the hero + uploader until a resume exists. If a previously
parsed profile is on disk, offer it as a fast path (returning users
shouldn't re-upload every session).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from backend.config import PROFILE_FILE
# resume_parser.api pulls in PyMuPDF (fitz) -- a native C extension that has
# no business loading at Streamlit boot for every tab/page. Deferred into
# _parse_and_advance() so it only loads when a resume is actually uploaded.


def render() -> None:
    st.markdown("""
<div class="uja-hero">
    <h1>Universal AI Job Agent</h1>
    <p>Your AI career coach. Upload your resume — get an honest analysis,<br>
    skill gaps computed from real job data, and jobs ranked for <em>you</em>.</p>
</div>""", unsafe_allow_html=True)

    _, center, _ = st.columns([1, 2, 1])
    with center:
        uploaded = st.file_uploader(
            "Upload your resume", type=["pdf"],
            label_visibility="collapsed",
            help="PDF only. Parsed locally — nothing leaves your machine.")

        if uploaded is not None:
            if st.button("Analyze Resume", type="primary",
                         use_container_width=True):
                _parse_and_advance(uploaded)

        if PROFILE_FILE.exists():
            st.markdown('<p class="uja-muted" style="text-align:center;'
                        'margin-top:1rem;">or</p>', unsafe_allow_html=True)
            if st.button("Continue with previously analyzed resume",
                         use_container_width=True):
                st.session_state.stage = "dashboard"
                st.rerun()


def _parse_and_advance(uploaded) -> None:
    from backend.resume_parser.api import parse_resume_pdf, save_profile
    with st.spinner("Parsing your resume..."):
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf",
                                             delete=False) as tmp:
                tmp.write(uploaded.getbuffer())
                tmp_path = Path(tmp.name)
            profile = parse_resume_pdf(tmp_path)
            tmp_path.unlink(missing_ok=True)
        except Exception as exc:
            st.error(f"Could not parse this PDF: {exc}")
            return

    if not profile.get("skills") and not profile.get("projects"):
        st.warning("Parsed the file but found no skills or projects — is this "
                   "a text-based (not scanned) resume PDF?")
        return

    save_profile(profile)          # backs up existing profile to .bak
    st.session_state.stage = "review"    # user confirms extraction first
    st.rerun()
