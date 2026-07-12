"""
review.py -- Review & Edit step between parsing and analysis.

The parser is imperfect, so the user confirms/fixes extracted data BEFORE
any scores or recommendations are computed. Per-section parse-confidence
badges tell them where to look; warnings quote the exact suspect item.

Edits rebuild the same candidate_profile.json schema the whole pipeline
already consumes -- no downstream changes needed. Skill categories/domains
are preserved for kept skills; user-added skills get a neutral category.
"""
from __future__ import annotations

import re

import streamlit as st

from backend.analysis.parse_confidence import compute_parse_confidence
from backend.resume_parser.api import save_profile
from .. import theme


def _conf_pill(pct: float) -> str:
    variant = "green" if pct >= 85 else "amber" if pct >= 60 else "red"
    return f'<span class="uja-pill {variant}">{pct:.0f}% confident</span>'


def _bullets_to_text(description: str) -> str:
    bullets = [b.strip() for b in re.split("•", description or "") if b.strip()]
    return "\n".join(bullets)


def _text_to_description(text: str) -> str:
    bullets = [line.strip().lstrip("-•").strip()
               for line in text.splitlines() if line.strip()]
    return " • " + " • ".join(bullets) if bullets else ""


def render(profile: dict) -> None:
    conf = compute_parse_confidence(profile)

    st.title("Review what we extracted")
    st.markdown(f'<p class="uja-muted">Overall parse confidence: '
                f'<b>{conf["overall"]:.0f}%</b>. Fix anything wrong below — '
                'accurate input means accurate recommendations.</p>',
                unsafe_allow_html=True)

    all_warnings = [w for s in conf["sections"].values() for w in s["warnings"]]
    if all_warnings:
        with st.expander(f"⚠️ {len(all_warnings)} thing(s) to double-check",
                         expanded=True):
            for w in all_warnings:
                st.markdown(f"- {w}")

    with st.form("review_form"):
        # ---- Contact ----
        st.markdown("#### Contact " + _conf_pill(
            conf["sections"]["Contact"]["confidence"]), unsafe_allow_html=True)
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input("Name", profile.get("name") or "")
            email = st.text_input("Email", profile.get("email") or "")
            phone = st.text_input("Phone", profile.get("phone") or "")
        with c2:
            linkedin = st.text_input("LinkedIn", profile.get("linkedin") or "")
            github = st.text_input("GitHub", profile.get("github") or "")

        # ---- Skills ----
        st.markdown("#### Skills " + _conf_pill(
            conf["sections"]["Skills"]["confidence"]), unsafe_allow_html=True)
        existing = {s["skill"]: s for s in profile.get("skills", [])}
        skills_text = st.text_area(
            "One skill per line (add missing ones, delete wrong ones)",
            "\n".join(existing.keys()), height=180)

        # ---- Education ----
        st.markdown("#### Education " + _conf_pill(
            conf["sections"]["Education"]["confidence"]), unsafe_allow_html=True)
        edu = (profile.get("education") or [{}])[0]
        e1, e2 = st.columns(2)
        with e1:
            degree = st.text_input("Degree", edu.get("degree") or "")
            college = st.text_input("Institution", edu.get("college") or "")
        with e2:
            years = st.text_input("Years", edu.get("years") or "")
            cgpa = st.text_input("CGPA / GPA", edu.get("cgpa") or "")

        # ---- Projects ----
        st.markdown("#### Projects " + _conf_pill(
            conf["sections"]["Projects"]["confidence"]), unsafe_allow_html=True)
        st.markdown('<p class="uja-muted">One bullet per line. Empty title = '
                    'project removed.</p>', unsafe_allow_html=True)
        edited_projects = []
        for i, p in enumerate(profile.get("projects", [])):
            st.markdown(f"**Project {i + 1}**")
            pc1, pc2 = st.columns([3, 1])
            with pc1:
                title = st.text_input("Title", p.get("title") or "",
                                      key=f"pt_{i}")
            with pc2:
                duration = st.text_input("Duration", p.get("duration") or "",
                                         key=f"pd_{i}")
            bullets_text = st.text_area(
                "Bullets", _bullets_to_text(p.get("description", "")),
                height=140, key=f"pb_{i}", label_visibility="collapsed")
            edited_projects.append((title, duration, bullets_text))

        submitted = st.form_submit_button("Save & Analyze →", type="primary",
                                          use_container_width=True)

    if submitted:
        skills = []
        for line in skills_text.splitlines():
            skill = line.strip()
            if not skill:
                continue
            kept = existing.get(skill)
            skills.append(kept or {"skill": skill, "category": "Other",
                                   "domain": "General"})

        projects = [
            {"title": t.strip(), "duration": d.strip(),
             "description": _text_to_description(b)}
            for t, d, b in edited_projects if t.strip()
        ]

        updated = {
            "name": name.strip(), "email": email.strip(),
            "phone": phone.strip(),
            "linkedin": linkedin.strip() or None,
            "github": github.strip() or None,
            "skills": skills,
            "education": [{"college": college.strip(), "degree": degree.strip(),
                           "cgpa": cgpa.strip(), "years": years.strip()}],
            "projects": projects,
        }
        save_profile(updated)
        st.cache_data.clear()          # profile changed -> recompute analysis
        st.session_state.profile_just_parsed = True
        st.session_state.stage = "dashboard"
        st.rerun()
