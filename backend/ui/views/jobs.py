"""
jobs.py -- Steps 9-10: recommended jobs (existing ranking engine, re-skinned).

Reads data/ranked_jobs.json when available (instant). If the profile was
just re-parsed or no ranked file exists, offers an explicit "Rank jobs now"
button -- ranking 3,875 jobs loads the embedding model and takes minutes,
so it must never run implicitly on page load.

Two ways a ranking runs (ADR-001 §2):
- Database configured (DATABASE_URL / DB_HOST): the buttons enqueue a "rank"
  task through services.ranking.request_ranking() -- the same path as
  POST /v1/rankings -- and the page polls the task's status. The worker
  (python -m backend.worker) is the only writer of ranked_jobs.json.
- No database (the Streamlit Cloud demo): _rerank_inline() ranks in this
  process via services.ranking.rank_and_publish(). It is the only
  in-process ranking path the UI has, and exists because a DB-less
  deployment has no queue and no worker.
"""
from __future__ import annotations

import json

import streamlit as st

from backend.config import RANKED_FILE
from backend.database.corpus import (db_configured, load_corpus,
                                     load_corpus_snapshot)
from .. import theme

RANK_POLL_S = 5          # status refresh while a queued ranking runs


@st.cache_data(show_spinner=False)
def _load_ranked(mtime: float) -> list[dict]:
    with open(RANKED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@st.cache_data(show_spinner=False)
def _load_descriptions(corpus_version: tuple, _jobs: list[dict]) -> dict:
    # Keyed on the corpus version (file mtime, or DB row count + last
    # sighting); the corpus itself is underscore-excluded from hashing.
    out = {}
    for j in _jobs:
        key = ((j.get("company") or "").lower().strip(),
               " ".join((j.get("title") or "").lower().split()))
        out.setdefault(key, j.get("clean_description", ""))
    return out


def _rerank_inline(profile: dict) -> None:
    """Demo mode only (no database configured): rank in this process,
    publishing through the ranking service like every other writer (an
    atomic replace, so two demo sessions reranking at once can't
    interleave)."""
    from backend.services.ranking import rank_and_publish
    jobs = load_corpus()
    with st.spinner(f"Ranking {len(jobs):,} jobs against your profile — "
                    "this takes a few minutes (embedding model)..."):
        rank_and_publish(profile, jobs)
    st.cache_data.clear()
    st.rerun()


def _request_rerank(profile: dict) -> None:
    """Rank on the worker when a database is configured; inline otherwise."""
    if not db_configured():
        _rerank_inline(profile)
        return
    from backend.database.corpus import CorpusUnavailable
    from backend.services.ranking import request_ranking
    from backend.taskqueue import ConcurrencyConflict
    try:
        task_id = request_ranking()
    except ConcurrencyConflict:
        notice = ("info", "A ranking run is already in progress.")
    except CorpusUnavailable as exc:
        notice = ("error", "The database has no job corpus yet. Import it with "
                  "`python -m backend.database.corpus import`."
                  if exc.reason == "empty" else
                  f"The ranking queue's database is unavailable: {exc}")
    except Exception as exc:                                # noqa: BLE001
        notice = ("error", f"Couldn't queue the ranking run: {exc}")
    else:
        notice = ("success", f"Queued ranking task #{task_id}. The worker "
                  "(`python -m backend.worker`) will pick it up.")
    st.session_state["rank_notice"] = notice
    st.rerun()                     # re-render the status panel above


def _rank_status() -> dict | None:
    from backend.services.ranking import ranking_status
    try:
        return ranking_status(RANKED_FILE.exists())
    except Exception as exc:                                # noqa: BLE001
        st.warning(f"Couldn't read the ranking queue: {exc}")
        return None


def _running_panel_body() -> None:
    status = _rank_status()
    if status is None or status["state"] != "running":
        st.rerun()                 # finished: reload the page's rankings
    st.info(f"⏳ Ranking in progress — {status['detail']}")


if hasattr(st, "fragment"):        # Streamlit >= 1.37: poll on a timer
    _running_panel = st.fragment(run_every=RANK_POLL_S)(_running_panel_body)
else:                              # older Streamlit: manual refresh
    def _running_panel() -> None:
        _running_panel_body()
        st.button("Refresh status", key="rank_refresh")


def _queue_status_panel() -> None:
    """Queue mode: the latest rank task's status; polls while it runs."""
    notice = st.session_state.pop("rank_notice", None)
    if notice:
        getattr(st, notice[0])(notice[1])
    status = _rank_status()
    if status is None:
        return
    if status["state"] == "running":
        st.session_state["rank_watching"] = True
        _running_panel()
    elif st.session_state.pop("rank_watching", False):
        if status["state"] == "done":
            st.success(f"Ranking finished: {status['ranked_count'] or 0:,} "
                       "jobs ranked.")
        elif status["state"] == "failed":
            st.error(f"Ranking failed: {status['detail']}")


def _top_n_input(n_ranked: int) -> int:
    """The "Show top N" picker: steps of 10, from 10 up to every ranked job.
    With fewer than 10 ranked jobs (the sample corpus after the gates, or a
    small DB corpus) that range would be empty -- min 10 > max n, which
    Streamlit rejects -- so it becomes 1..n in steps of 1 (0..0 when no job
    passed the gates)."""
    if n_ranked >= 10:
        return st.number_input("Show top N", 10, n_ranked, min(30, n_ranked), 10)
    return st.number_input("Show top N", min(1, n_ranked), n_ranked, n_ranked, 1)


def render(profile: dict) -> None:
    corpus = load_corpus_snapshot()      # DB -> jobs.json -> sample (cached)
    has_corpus = corpus.source != "none"
    if corpus.source == "sample":
        st.info("Running on the sample corpus (120 jobs) — this deployment "
                "doesn't have the full scraped dataset. Run "
                "`python -m backend.job_scraper.ats_collector` locally for "
                "the full 3,875-job corpus.")

    if st.session_state.pop("profile_just_parsed", False) and RANKED_FILE.exists():
        st.info("Your resume was re-analyzed. Rankings below may be from the "
                "previous profile — re-rank to refresh.")

    if db_configured():
        _queue_status_panel()

    if not RANKED_FILE.exists():
        st.markdown('<p class="uja-muted">No job rankings yet.</p>',
                    unsafe_allow_html=True)
        if has_corpus and st.button("Rank jobs now", type="primary"):
            _request_rerank(profile)
        if not has_corpus:
            st.error("No jobs.json found. Run the collector first: "
                     "`python -m backend.job_scraper.ats_collector`")
        return

    ranked = _load_ranked(RANKED_FILE.stat().st_mtime)
    descriptions = (_load_descriptions(corpus.version, corpus.jobs)
                    if has_corpus else {})

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
        top_n = _top_n_input(len(ranked))
    if st.button("Re-rank against current resume"):
        _request_rerank(profile)

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
