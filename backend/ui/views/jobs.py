"""
jobs.py -- Steps 9-10: recommended jobs (existing ranking engine, re-skinned).

Reads data/ranked_jobs.json when available (instant). If the profile was
just re-parsed or no ranked file exists, offers an explicit "Rank jobs now"
button -- ranking 3,875 jobs loads the embedding model and takes minutes,
so it must never run implicitly on page load.
"""
from __future__ import annotations

import json

import streamlit as st

from backend.config import RANKED_FILE, JOBS_FILE
from .. import theme


@st.cache_data(show_spinner=False)
def _load_ranked(mtime: float) -> list[dict]:
    with open(RANKED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(show_spinner=False)
def _load_descriptions(mtime: float) -> dict:
    with open(JOBS_FILE, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    out = {}
    for j in jobs:
        key = ((j.get("company") or "").lower().strip(),
               " ".join((j.get("title") or "").lower().split()))
        out.setdefault(key, j.get("clean_description", ""))
    return out


def _rerank_inline(profile: dict) -> None:
    from backend.matching_engine.matcher import rank_jobs
    with open(JOBS_FILE, "r", encoding="utf-8") as f:
        jobs = json.load(f)
    with st.spinner(f"Ranking {len(jobs):,} jobs against your profile — "
                    "this takes a few minutes (embedding model)..."):
        ranked = rank_jobs(profile, jobs, candidate_is_fresher=True)
    with open(RANKED_FILE, "w", encoding="utf-8") as f:
        json.dump(ranked, f, ensure_ascii=False, indent=2)
    st.cache_data.clear()
    st.rerun()


def render(profile: dict) -> None:
    if st.session_state.pop("profile_just_parsed", False) and RANKED_FILE.exists():
        st.info("Your resume was re-analyzed. Rankings below may be from the "
                "previous profile — re-rank to refresh.")

    if not RANKED_FILE.exists():
        st.markdown('<p class="uja-muted">No job rankings yet.</p>',
                    unsafe_allow_html=True)
        if JOBS_FILE.exists() and st.button("Rank jobs now", type="primary"):
            _rerank_inline(profile)
        if not JOBS_FILE.exists():
            st.error("No jobs.json found. Run the collector first: "
                     "`python -m backend.job_scraper.ats_collector`")
        return

    ranked = _load_ranked(RANKED_FILE.stat().st_mtime)
    descriptions = (_load_descriptions(JOBS_FILE.stat().st_mtime)
                    if JOBS_FILE.exists() else {})

    # Filters
    f1, f2, f3, f4 = st.columns([1, 1, 1, 1])
    with f1:
        min_score = st.slider("Min score", 0.0, 100.0, 0.0, 1.0)
    with f2:
        companies = sorted({r.get("company", "") for r in ranked
                            if r.get("company")})
        company_filter = st.multiselect("Company", companies)
    with f3:
        search = st.text_input("Search title")
    with f4:
        top_n = st.number_input("Show top N", 10, len(ranked),
                                min(30, len(ranked)), 10)
    if st.button("Re-rank against current resume"):
        _rerank_inline(profile)

    filtered = [r for r in ranked if r["match_score"] >= min_score]
    if company_filter:
        filtered = [r for r in filtered if r.get("company") in company_filter]
    if search:
        filtered = [r for r in filtered
                    if search.lower() in (r.get("title") or "").lower()]

    st.markdown(f'<p class="uja-muted">{len(filtered):,} of {len(ranked):,} '
                'ranked jobs match your filters</p>', unsafe_allow_html=True)

    for r in filtered[:top_n]:
        _job_card(r, ranked, descriptions)


def _job_card(r: dict, ranked: list[dict], descriptions: dict) -> None:
    overall_rank = ranked.index(r) + 1
    b = r["score_breakdown"]

    st.markdown('<div class="uja-card">', unsafe_allow_html=True)
    head_l, head_r = st.columns([4, 1])
    with head_l:
        st.markdown(f"**{r['title']}**  \n"
                    f'<span class="uja-muted">{r["company"]}'
                    f'{" · " + r["location"] if r.get("location") else ""}'
                    '</span>', unsafe_allow_html=True)
    with head_r:
        st.markdown(f'<div class="uja-metric" style="font-size:1.7rem;'
                    f'text-align:right;">{r["match_score"]:.0f}</div>'
                    '<p class="uja-muted" style="text-align:right;">match</p>',
                    unsafe_allow_html=True)

    if r.get("strong_matches"):
        st.markdown("Strong: " + theme.pills(r["strong_matches"][:8], "green"),
                    unsafe_allow_html=True)
    if r.get("missing_skills"):
        st.markdown("Missing: " + theme.pills(r["missing_skills"][:6], "gray"),
                    unsafe_allow_html=True)

    with st.expander("Details"):
        for name, weight in [("skill_overlap", 0.40), ("semantic", 0.30),
                             ("role", 0.20), ("seniority", 0.10)]:
            val = b.get(name, 0.0)
            st.progress(min(max(val, 0.0), 1.0),
                        text=f"{name} ({weight:.0%}): {val:.2f}")
        key = ((r.get("company") or "").lower().strip(),
               " ".join((r.get("title") or "").lower().split()))
        jd = descriptions.get(key, "")
        if jd:
            st.text_area("Job description", jd[:5000], height=200,
                         disabled=True, key=f"jd_{overall_rank}")

    a1, a2, a3 = st.columns(3)
    with a1:
        if r.get("apply_link"):
            st.link_button("Apply ↗", r["apply_link"],
                           use_container_width=True)
    with a2:
        with st.popover("Optimize resume", use_container_width=True):
            _tailor_widget(r, overall_rank, descriptions)
    with a3:
        with st.popover("Cover letter", use_container_width=True):
            _cover_letter_widget(r, overall_rank, descriptions)
    st.markdown('</div>', unsafe_allow_html=True)


def _jd_for(r: dict, descriptions: dict) -> str:
    key = ((r.get("company") or "").lower().strip(),
           " ".join((r.get("title") or "").lower().split()))
    return descriptions.get(key, "")


def _load_current_profile() -> dict:
    import json as _json
    from backend.config import PROFILE_FILE
    with open(PROFILE_FILE, "r", encoding="utf-8") as f:
        return _json.load(f)


def _tailor_widget(r: dict, rank: int, descriptions: dict) -> None:
    """Runs the tailoring pipeline in-app: LLM rewrite -> hallucination
    validation -> ATS-safe .docx -> download button."""
    from pathlib import Path
    state_key = f"tailored_{rank}"
    st.caption("Rewrites your project bullets to mirror this JD — every "
               "bullet verified against your real resume; fabrications "
               "fall back to your original wording.")
    if st.button("Generate tailored resume", key=f"btn_t_{rank}",
                 use_container_width=True):
        from backend.resume_engine.resume_tailor import (
            tailor_to_job, OllamaUnavailableError)
        try:
            with st.spinner("Tailoring — JD analysis, rewrite, and "
                            "truthfulness checks in progress..."):
                path, validation = tailor_to_job(
                    _load_current_profile(), r, _jd_for(r, descriptions))
            st.session_state[state_key] = (str(path), validation)
        except OllamaUnavailableError as exc:
            st.error(str(exc))          # provider explains the exact fix
        except RuntimeError as exc:
            st.error(str(exc))

    if state_key in st.session_state:
        path, validation = st.session_state[state_key]
        for v in validation:
            if v["ok"]:
                st.markdown(f"✅ {v['project']}: verified truthful")
            else:
                st.markdown(f"⚠️ {v['project']}: LLM invented "
                            f"{', '.join(v['flagged'][:4])} — kept your "
                            "original bullets")
        with open(path, "rb") as f:
            st.download_button("⬇ Download resume (.docx)", f.read(),
                               file_name=Path(path).name,
                               key=f"dl_t_{rank}", use_container_width=True)


def _cover_letter_widget(r: dict, rank: int, descriptions: dict) -> None:
    from pathlib import Path
    state_key = f"cover_{rank}"
    st.caption("Drafts a 3-paragraph letter grounded ONLY in your real "
               "projects, with a truthfulness review.")
    if st.button("Generate cover letter", key=f"btn_c_{rank}",
                 use_container_width=True):
        from backend.resume_engine.cover_letter import generate_for_job
        from backend.resume_engine.resume_tailor import OllamaUnavailableError
        try:
            with st.spinner("Drafting and reviewing the letter..."):
                path, paragraphs, review = generate_for_job(
                    _load_current_profile(), r, _jd_for(r, descriptions))
            st.session_state[state_key] = (str(path), paragraphs, review)
        except OllamaUnavailableError as exc:
            st.error(str(exc))          # provider explains the exact fix
        except RuntimeError as exc:
            st.error(str(exc))

    if state_key in st.session_state:
        path, paragraphs, review = st.session_state[state_key]
        for p in paragraphs:
            st.markdown(f'<span class="uja-muted">{p}</span>',
                        unsafe_allow_html=True)
        if review:
            for item in review:
                st.warning(f"Paragraph {item['paragraph']}: verify "
                           f"{', '.join(str(x) for x in item['flags'][:4])} "
                           "before sending")
        else:
            st.markdown("✅ All claims verified against your resume")
        with open(path, "rb") as f:
            st.download_button("⬇ Download cover letter", f.read(),
                               file_name=Path(path).name,
                               key=f"dl_c_{rank}", use_container_width=True)
