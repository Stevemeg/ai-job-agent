"""
inbox.py -- the approval Inbox (Phase 5). Database mode only: app.py shows
the tab only when Postgres is configured and reachable, so the DB-less
demo never sees it.

Every application awaiting approval, best match first: why it matched and
the gaps, the truthfulness validator's flags (highlighted), the cover
letter and resume bullets, the .docx downloads and the apply link -- then
Approve, Edit or Decline (with a reason).

A decision goes through backend.agent.decisions.submit_decision(), the
same function the API's POST /v1/applications/{id}/decision uses: it is
recorded with the version shown here (a stale page gets a conflict, not a
wrong decision) and the worker applies it. Approving prepares the packet
and at most a Gmail DRAFT; nothing is ever sent or submitted -- you apply
yourself, from the apply link.

Follow-ups due (Phase 6): applications you logged as applied
AGENT_FOLLOW_UP_DAYS ago with no reply logged since, with the follow-up
Gmail draft if one was made (never sent). Logging the reply in the
Tracker takes one off the list.

Memory (Phase 6): what the agent remembers about your preferences, stated
by you or learned from your decisions, with the evidence behind each one.
Edit, deactivate or delete them here (backend.agent.memory, the same
functions as /v1/memories), or state a new one. Memories only steer
triage: they never change a score.
"""
from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st

from .. import theme

DOWNLOADS = (("resume_docx", "Resume (.docx)"), ("cover_letter_docx", "Cover letter (.docx)"))
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MEMORY_LABELS = {"company_avoid": "Avoid company", "title_avoid": "Avoid title word",
                 "company_prefer": "Prefer company", "title_prefer": "Prefer title word",
                 "location_prefer": "Prefer location", "note": "Note"}


# ---- what a card shows (no Streamlit) -------------------------------------------

def flag_lines(artifacts: dict, documents: dict | None) -> list[str]:
    """Every validator flag on the current version, as one line each."""
    lines = []
    resume = (artifacts.get("resume_docx") or {}).get("report") or {}
    for p in resume.get("projects") or []:
        if not p.get("ok", True):
            kept = "kept as you wrote it" if p.get("edited") else "original bullets used"
            lines.append(f"Resume, {p['project']}: unverified {', '.join(p['flagged'])} "
                         f"({kept})")
    letter = (artifacts.get("cover_letter_docx") or {}).get("report") or {}
    for r in letter.get("paragraphs") or []:
        lines.append(f"Cover letter, paragraph {r['paragraph']}: unverified "
                     f"{', '.join(map(str, r['flags']))}")
    return lines


def flagged_paragraphs(artifacts: dict) -> set[int]:
    letter = (artifacts.get("cover_letter_docx") or {}).get("report") or {}
    return {r["paragraph"] for r in letter.get("paragraphs") or []}


def edits_from_form(documents: dict, paragraphs: list[str],
                    bullets: dict[str, str]) -> dict:
    """The edits a submitted form makes: only what changed. An emptied
    paragraph is dropped; bullets are one per line."""
    edits = {}
    new_paragraphs = [" ".join(p.split()) for p in paragraphs if p.strip()]
    if new_paragraphs != [" ".join(p.split()) for p in documents["cover_letter_paragraphs"]]:
        edits["cover_letter_paragraphs"] = new_paragraphs
    changed = {}
    for project in documents["resume_projects"]:
        lines = [" ".join(b.split()) for b in bullets.get(project["title"], "").splitlines()
                 if b.strip()]
        if lines != [" ".join(b.split()) for b in project["bullets"]]:
            changed[project["title"]] = lines
    if changed:
        edits["resume_bullets"] = changed
    return edits


def memory_label(m: dict) -> str:
    """One line for a memory: what it is, its value, where it came from."""
    parts = [f"{MEMORY_LABELS.get(m['kind'], m['kind'])}: {m['value']}",
             "stated by you" if m["provenance"] == "stated" else "learned"]
    if m["kind"] != "note":
        parts.append(f"weight {m['weight']:g}")
    if not m.get("active", True):
        parts.append("inactive")
    return " · ".join(parts)


def follow_up_line(app: dict) -> str:
    """One line for a due follow-up: the job and what was prepared."""
    draft = app.get("follow_up_draft") or {}
    if draft.get("status") == "done":
        prepared = "a follow-up Gmail draft is ready (not sent)"
    elif draft.get("status") == "pending":
        prepared = "the follow-up Gmail draft is being created"
    elif draft.get("status") == "failed":
        prepared = "the follow-up Gmail draft failed"
    else:
        prepared = "no draft (no contact email in the posting, or Gmail is off)"
    return (f"{app['title']} at {app['company']} ({app.get('location') or 'location n/a'})"
            f" -- {prepared}")


def evidence_lines(m: dict) -> list[str]:
    """What a memory rests on: the rule and each decision behind it (ids
    and decline reasons), or that you stated it."""
    ev = m.get("evidence") or {}
    lines = []
    if m["provenance"] == "stated":
        lines.append(f"You stated this (via {ev.get('stated_via', 'the API')}).")
    if ev.get("rule"):
        lines.append(("Learned first: " if m["provenance"] == "stated" else "Learned: ")
                     + ev["rule"] + ".")
    for item in (ev.get("declined") or []) + (ev.get("approved") or []):
        verb = "Declined" if item.get("decision") == "decline" else "Approved"
        ids = f"approval #{item.get('approval_id')}"
        if item.get("event_id") is not None:
            ids += f", event #{item['event_id']}"
        line = (f"{verb} application #{item.get('application_id')}: {item.get('title')} "
                f"at {item.get('company')} ({ids})")
        if item.get("reason"):
            line += f' -- "{item["reason"]}"'
        lines.append(line)
    return lines


# ---- Streamlit ------------------------------------------------------------------------

def _entry(app: dict) -> dict:
    from backend.agent import application_graph, decisions, store
    try:
        documents = application_graph.documents(application_graph.current_state(app["id"]))
    except Exception:                                       # noqa: BLE001
        documents = None
    return {"artifacts": {a["kind"]: a for a in store.get_artifacts(app["id"])},
            "match": decisions.explanation(app) or {},
            "documents": documents,
            "approvals": decisions.list_approvals(app["id"])}


def _submit(app: dict, action: str, **kwargs) -> None:
    from backend.agent import decisions
    try:
        decisions.submit_decision(app["id"], action=action, version=app["version"], **kwargs)
    except decisions.FlaggedEdit as exc:
        st.error("The edit has unverified claims. Fix them, or tick "
                 "\"Keep flagged text\" to keep them (they stay flagged):\n\n"
                 + "\n".join(f"- {_flag_text(f)}" for f in exc.flags))
        return
    except decisions.DecisionError as exc:
        st.warning(str(exc))
        return
    st.session_state["inbox_notice"] = (f"{action.title()} recorded for "
                                        f"{app['title']} — the worker is applying it.")
    st.rerun()


def _flag_text(flag: dict) -> str:
    where = (f"paragraph {flag['paragraph']}" if flag["document"] == "cover_letter"
             else flag.get("project", "resume"))
    return f"{where}: {', '.join(map(str, flag['flags']))}"


def _card(app: dict) -> None:
    entry = _entry(app)
    artifacts, documents, match = entry["artifacts"], entry["documents"], entry["match"]
    explanation = match.get("explanation") or {}
    pending = [a for a in entry["approvals"] if a["application_version"] == app["version"]]
    flags = flag_lines(artifacts, documents)

    review = (' <span class="uja-pill red">Needs review</span>'
              if app.get("needs_review") else ' <span class="uja-pill green">Validated</span>')
    score = app.get("match_score")
    st.markdown(
        f'<div class="uja-card"><span class="uja-pill">'
        f'{"—" if score is None else f"{score:.0f}"}</span> '
        f'<b>{escape(app["title"])}</b> — {escape(app["company"])}'
        f'<span class="uja-muted"> · {escape(app.get("location") or "location n/a")}'
        f' · version {app["version"]}</span>{review}</div>', unsafe_allow_html=True)

    with st.expander("Review", expanded=False):
        strong = explanation.get("strong_matches") or []
        likely = explanation.get("likely_matches") or []
        gaps = explanation.get("missing_skills") or []
        if strong or likely:
            st.markdown("**Why it matched** " + theme.pills(strong, "green")
                        + " " + theme.pills(likely), unsafe_allow_html=True)
        if gaps:
            st.markdown("**Gaps** " + theme.pills(gaps, "amber"), unsafe_allow_html=True)
        if flags:
            st.markdown("**Validator flags**", unsafe_allow_html=True)
            for line in flags:
                st.markdown(f'<span class="uja-pill red">flag</span> {escape(line)}',
                            unsafe_allow_html=True)
        else:
            st.markdown('<span class="uja-muted">The truthfulness validator found '
                        'nothing to flag.</span>', unsafe_allow_html=True)

        if documents:
            st.markdown("**Cover letter**")
            marked = flagged_paragraphs(artifacts)
            for i, p in enumerate(documents["cover_letter_paragraphs"], 1):
                style = (f"border-left:4px solid {theme.RED};padding-left:0.6rem;"
                         if i in marked else "")
                st.markdown(f'<p style="{style}">{escape(p)}</p>', unsafe_allow_html=True)
            st.markdown("**Resume bullets**")
            for project in documents["resume_projects"]:
                st.markdown(f"*{project['title']}*")
                for b in project["bullets"]:
                    st.markdown(f"- {b}")

        cols = st.columns(3)
        for col, (kind, label) in zip(cols, DOWNLOADS):
            path = Path((artifacts.get(kind) or {}).get("path") or "")
            with col:
                if path.is_file():
                    st.download_button(label, path.read_bytes(),
                                       file_name=f"{app['company']}-{path.name}",
                                       mime=DOCX_MIME, key=f"dl-{kind}-{app['id']}-{app['version']}")
        with cols[2]:
            if app.get("apply_link"):
                st.link_button("Apply link ↗", app["apply_link"])

        if pending:
            st.info(f"Your {pending[-1]['decision']} on version {app['version']} is "
                    "recorded; the worker is applying it.")
            return

        key = f"{app['id']}-{app['version']}"
        a_col, d_col = st.columns([1, 2])
        with a_col:
            if st.button("Approve", type="primary", key=f"approve-{key}"):
                _submit(app, "approve")
        with d_col:
            reason = st.text_input("Reason (optional)", key=f"reason-{key}",
                                   placeholder="e.g. not my field, too senior")
            if st.button("Decline", key=f"decline-{key}"):
                _submit(app, "decline", reason=reason)

        if documents:
            with st.form(f"edit-{key}"):
                st.markdown("**Edit** — your text is re-checked by the validator")
                paragraphs = [st.text_area(f"Paragraph {i}", value=p, key=f"p{i}-{key}")
                              for i, p in enumerate(documents["cover_letter_paragraphs"], 1)]
                bullets = {project["title"]: st.text_area(
                    f"{project['title']} (one bullet per line)",
                    value="\n".join(project["bullets"]), key=f"b-{project['title']}-{key}")
                    for project in documents["resume_projects"]}
                keep = st.checkbox("Keep flagged text (acknowledge validator flags)",
                                   key=f"ack-{key}")
                if st.form_submit_button("Save edit"):
                    edits = edits_from_form(documents, paragraphs, bullets)
                    if not edits:
                        st.warning("Nothing changed.")
                    else:
                        _submit(app, "edit", edits=edits, acknowledge_flags=keep)


def _load_follow_ups(profile: dict) -> list[dict]:
    from backend.agent import followups, store
    return followups.list_follow_ups_due(store.user_for_profile(profile))


def _follow_ups_section(profile: dict) -> None:
    try:
        due = _load_follow_ups(profile)
    except Exception as exc:                                # noqa: BLE001
        st.caption(f"Follow-ups are unavailable ({exc.__class__.__name__}).")
        return
    if not due:
        return
    st.markdown("---")
    st.markdown(f"#### Follow-ups due ({len(due)})")
    st.markdown('<p class="uja-muted">You logged these as applied a while ago and no '
                'reply since. Nothing was sent: send the draft yourself if you want to. '
                'Logging the reply (interview, rejection, offer) in the Tracker takes '
                'one off this list.</p>', unsafe_allow_html=True)
    for app in due:
        st.markdown(f'<span class="uja-pill amber">follow up</span> '
                    f'{escape(follow_up_line(app))}', unsafe_allow_html=True)
        if app.get("apply_link"):
            st.link_button("Posting ↗", app["apply_link"])


def _load_memories(profile: dict) -> tuple[int, list[dict]]:
    from backend.agent import memory, store
    user_id = store.user_for_profile(profile)
    return user_id, memory.list_memories(user_id)


def _memory_action(fn, notice: str, **kwargs) -> None:
    from backend.agent import memory
    try:
        fn(**kwargs)
    except memory.MemoryRequestError as exc:
        st.warning(str(exc))
        return
    st.session_state["inbox_notice"] = notice
    st.rerun()


def _memory_row(user_id: int, m: dict) -> None:
    from backend.agent import memory
    pill = "green" if m["provenance"] == "stated" else "amber"
    off = "" if m.get("active", True) else ' <span class="uja-pill">inactive</span>'
    st.markdown(f'<span class="uja-pill {pill}">{escape(m["provenance"])}</span> '
                f'{escape(memory_label(m))}{off}', unsafe_allow_html=True)
    key = f"mem-{m['id']}"
    with st.expander("Evidence and edit", expanded=False):
        for line in evidence_lines(m):
            st.markdown(f"- {escape(line)}", unsafe_allow_html=True)
        with st.form(f"{key}-form"):
            value = st.text_input("Value", value=m["value"], key=f"{key}-value")
            weight = (None if m["kind"] == "note" else
                      st.number_input("Weight (prefer: boost points)", min_value=0.0,
                                      max_value=100.0, value=float(m["weight"]),
                                      step=0.5, key=f"{key}-weight"))
            if st.form_submit_button("Save"):
                _memory_action(memory.update, f"Memory #{m['id']} saved.",
                               user_id=user_id, memory_id=m["id"], value=value,
                               weight=weight)
        a_col, d_col = st.columns(2)
        with a_col:
            label = "Deactivate" if m.get("active", True) else "Activate"
            if st.button(label, key=f"{key}-toggle"):
                _memory_action(memory.update, f"Memory #{m['id']} {label.lower()}d.",
                               user_id=user_id, memory_id=m["id"],
                               active=not m.get("active", True))
        with d_col:
            if st.button("Delete", key=f"{key}-delete"):
                _memory_action(memory.delete, f"Memory #{m['id']} deleted.",
                               user_id=user_id, memory_id=m["id"])


def _memory_section(profile: dict) -> None:
    from backend.agent import memory
    st.markdown("---")
    st.markdown("#### Memory")
    st.markdown('<p class="uja-muted">What the agent remembers about your preferences. '
                'An avoid memory keeps matching jobs out of triage; a prefer memory '
                'moves matching jobs up the order. Neither ever changes a score or lets '
                'a job past AGENT_MIN_SCORE. Learned memories come from your approvals '
                'and declines; deactivate one to stop it being learned again.</p>',
                unsafe_allow_html=True)
    try:
        user_id, memories = _load_memories(profile)
    except Exception as exc:                                # noqa: BLE001
        st.caption(f"Memory is unavailable ({exc.__class__.__name__}).")
        return
    if not memories:
        st.markdown('<p class="uja-muted">Nothing remembered yet.</p>',
                    unsafe_allow_html=True)
    for m in memories:
        _memory_row(user_id, m)
    with st.form("mem-add"):
        st.markdown("**Tell the agent something**")
        kind = st.selectbox("Kind", list(MEMORY_LABELS), format_func=MEMORY_LABELS.get,
                            key="mem-add-kind")
        value = st.text_input("Value", key="mem-add-value",
                              placeholder="e.g. a company, a title word, a city")
        if st.form_submit_button("Remember"):
            _memory_action(memory.create_stated, "Remembered.", user_id=user_id,
                           kind=kind, value=value, via="ui")


def render(profile: dict) -> None:
    try:
        from backend.agent import store
        apps = store.list_applications(status="awaiting_approval", sort="score", limit=100)
    except Exception as exc:                                # noqa: BLE001
        st.markdown('<p class="uja-muted">The inbox reads the agent\'s applications '
                    'from PostgreSQL, which isn\'t reachable right now.</p>',
                    unsafe_allow_html=True)
        with st.expander("Error detail"):
            st.caption(str(exc))
        return

    notice = st.session_state.pop("inbox_notice", None)
    if notice:
        st.success(notice)
    st.markdown(f"**{len(apps)}** application{'s' if len(apps) != 1 else ''} awaiting "
                "your decision, best match first. Approving prepares the packet (and "
                "at most a Gmail draft); nothing is ever sent or submitted.")
    if not apps:
        st.markdown('<p class="uja-muted">Nothing to review. The agent drafts for new '
                    'matches after each scan (AGENT_ENABLED=true) or on '
                    'POST /v1/agent/runs.</p>', unsafe_allow_html=True)
    for app in apps:
        _card(app)
    _follow_ups_section(profile)
    _memory_section(profile)
