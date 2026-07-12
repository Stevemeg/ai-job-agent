"""
tracker.py -- Application Tracker tab.

Pipeline board + event logging + per-job timeline, over the Phase 2 events
database. Requires Postgres (docker compose up in backend/database);
degrades to clear instructions when the DB is unreachable rather than
crashing the rest of the dashboard.
"""
from __future__ import annotations

import json

import streamlit as st

from backend.config import RANKED_FILE
from .. import theme

STATUS_META = {
    "save":      ("Saved", ""),
    "applied":   ("Applied", "amber"),
    "interview": ("Interview", "green"),
    "offer":     ("Offer 🎉", "green"),
    "rejected":  ("Rejected", "red"),
    "dismiss":   ("Dismissed", "gray"),
}
LOGGABLE = ["save", "applied", "interview", "rejected", "offer", "dismiss"]


def _db_available() -> tuple[bool, str]:
    try:
        from backend.database.db import init_db
        init_db()                      # also runs additive migrations (note col)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def render(profile: dict) -> None:
    ok, err = _db_available()
    if not ok:
        st.markdown('<p class="uja-muted">The tracker stores your application '
                    'history in PostgreSQL, which isn\'t reachable right now.'
                    '</p>', unsafe_allow_html=True)
        st.code("cd backend/database\ndocker compose up -d", language="powershell")
        with st.expander("Error detail"):
            st.caption(err)
        return

    from backend.database.tracker import (get_or_create_user, get_tracked_jobs,
                                          get_funnel_counts)
    from backend.database.events import log_outcome

    user_id = get_or_create_user(profile.get("email") or "local@user",
                                 profile.get("name", ""))
    tracked = get_tracked_jobs(user_id)

    # ---- funnel summary ------------------------------------------------------
    counts = get_funnel_counts(tracked)
    c1, c2, c3, c4, c5 = st.columns(5)
    for col, key, label in [(c1, "save", "Saved"), (c2, "applied", "Applied"),
                            (c3, "interview", "Interviews"),
                            (c4, "offer", "Offers"), (c5, "rejected", "Rejected")]:
        with col:
            theme.card(f'<span class="uja-metric" style="font-size:1.6rem;">'
                       f'{counts[key]}</span><br>'
                       f'<span class="uja-muted">{label}</span>')

    # ---- log an event --------------------------------------------------------
    with st.expander("➕ Log an application event", expanded=not tracked):
        ranked = []
        if RANKED_FILE.exists():
            with open(RANKED_FILE, "r", encoding="utf-8") as f:
                ranked = json.load(f)

        with st.form("log_event", clear_on_submit=True):
            source = None
            if ranked:
                options = ["— type manually —"] + [
                    f"{r['title']} @ {r['company']}" for r in ranked[:200]]
                pick = st.selectbox("Job (from your ranked list)", options)
                if pick != options[0]:
                    source = ranked[options.index(pick) - 1]
            mc1, mc2 = st.columns(2)
            with mc1:
                company = st.text_input(
                    "Company", value=(source or {}).get("company", ""))
            with mc2:
                title = st.text_input(
                    "Exact title", value=(source or {}).get("title", ""))
            ec1, ec2 = st.columns([1, 2])
            with ec1:
                event_type = st.selectbox("Event", LOGGABLE, index=1)
            with ec2:
                note = st.text_input("Note (optional)",
                                     placeholder="e.g. referred by X; phone screen Fri")
            if st.form_submit_button("Log event", type="primary"):
                if not company.strip() or not title.strip():
                    st.error("Company and title are required.")
                else:
                    log_outcome(
                        user_id, company.strip(), title.strip(), event_type,
                        match_score=(source or {}).get("match_score"),
                        score_breakdown=(source or {}).get("score_breakdown"),
                        note=note.strip() or None)
                    st.rerun()

    if not tracked:
        st.markdown('<p class="uja-muted">Nothing tracked yet. Log your first '
                    'application above — every outcome you record is also '
                    'future training data for learning-to-rank.</p>',
                    unsafe_allow_html=True)
        return

    # ---- board ---------------------------------------------------------------
    active = [t for t in tracked if t["status"] not in ("dismiss",)]
    show_dismissed = st.toggle("Show dismissed", value=False)
    if show_dismissed:
        active = tracked

    for t in active:
        label, variant = STATUS_META[t["status"]]
        score = (f' · match {t["match_score"]:.0f}' if t["match_score"] else "")
        st.markdown('<div class="uja-card">', unsafe_allow_html=True)
        head_l, head_r = st.columns([4, 1])
        with head_l:
            st.markdown(
                f'<span class="uja-pill {variant}">{label}</span> '
                f'<b>{t["title"]}</b> — {t["company"]}'
                f'<span class="uja-muted">{score}</span>',
                unsafe_allow_html=True)
        with head_r:
            st.markdown(f'<span class="uja-muted">'
                        f'{t["last_activity"]:%d %b %Y}</span>',
                        unsafe_allow_html=True)
        with st.expander("Timeline"):
            for h in t["history"]:                      # newest first
                note = f' — <i>{h["note"]}</i>' if h["note"] else ""
                st.markdown(
                    f'<span class="uja-muted">{h["created_at"]:%d %b %Y %H:%M}'
                    f'</span> · <b>{h["event_type"]}</b>{note}',
                    unsafe_allow_html=True)
            if t.get("apply_link"):
                st.link_button("Job posting ↗", t["apply_link"])
        st.markdown('</div>', unsafe_allow_html=True)
