"""
analysis.py -- the Resume Intelligence Dashboard (Steps 3-8).

Renders: personalized hero, health score with per-dimension explanations,
candidate summary, grouped skills, role-weighted skill gaps, evidence-backed
suggestions, and explained career fits. All numbers come from
backend.analysis (pure functions); this file only lays them out.
"""
from __future__ import annotations

import streamlit as st

from backend.analysis.resume_health import compute_health
from backend.analysis.skill_gap import compute_skill_gaps
from backend.analysis.career_recommender import recommend_careers
from backend.analysis.suggestions import generate_suggestions
from backend.matching_engine.domain_filter import detect_candidate_domains
from .. import theme

DOMAIN_LABELS = {"ai_ml": "AI / ML", "data_science": "Data Science",
                 "web_dev": "Web Development"}

# How each health dimension is computed -- shown to the user so the number
# is never a black box.
HEALTH_EXPLANATIONS = {
    "Contact & Links": "Name, email, phone, LinkedIn, GitHub present "
                       "(weighted 3/4/3/3/2 of 15).",
    "Skills": "Up to 12 pts for breadth (15+ skills = max) plus 8 pts for "
              "category diversity (8+ categories = max).",
    "Projects": "8 pts for having 3+ projects, 4 pts for real titles, "
                "8 pts for 3+ bullets each.",
    "Quantified Impact": "Share of project bullets containing a number, %, "
                         "or metric × 15.",
    "Education": "Degree (4), institution (3), years (2), GPA (1).",
    "ATS Readiness": "8 pts × share of bullets starting with an action verb, "
                     "6 pts × share of bullets 8-40 words, 6 pts for having "
                     "skills+education+projects sections.",
}


# ---- hero: the immediate takeaway -------------------------------------------

def render_hero(profile: dict, results: dict) -> None:
    first_name = (profile.get("name") or "there").split()[0]
    careers = results["careers"]
    gaps = results["gaps"]

    strongest = ", ".join(c["role"] for c in careers[:3]) or "—"
    hero = [f"<h3 style='margin-bottom:0.3rem;'>Hello {first_name} 👋</h3>",
            f"Your resume is strongest for: <b>{strongest}</b>"]

    top_gaps = [g for g in gaps if g["priority"] in ("High", "Medium")][:2]
    if top_gaps:
        names = " + ".join(g["skill"] for g in top_gaps)
        basis = top_gaps[0]
        if basis.get("role_weighted"):
            hero.append(
                f"Your biggest opportunity: <b>learn {names}</b> — "
                f"{basis['skill']} appears in <b>{basis['role_demand_pct']}%"
                f"</b> of your target-role postings "
                f"({basis['role_jobs']:,} jobs analyzed).")
        else:
            hero.append(
                f"Your biggest opportunity: <b>learn {names}</b> — "
                f"{basis['skill']} appears in <b>{basis['demand_pct']}%</b> "
                "of all collected jobs.")
    theme.card("<br>".join(hero))


# ---- Step 3+4: overview -----------------------------------------------------

def render_overview(profile: dict, health: dict) -> None:
    col_ring, col_breakdown = st.columns([1, 2])

    with col_ring:
        st.markdown('<div class="uja-card" style="text-align:center;">',
                    unsafe_allow_html=True)
        st.markdown('<p class="uja-muted" style="margin-bottom:0.8rem;">'
                    'Resume Health Score</p>', unsafe_allow_html=True)
        theme.score_ring(health["score"], health["grade"])
        st.markdown('</div>', unsafe_allow_html=True)

    with col_breakdown:
        st.markdown('<div class="uja-card">', unsafe_allow_html=True)
        st.markdown("**Score breakdown** — expand any line to see exactly "
                    "how it's calculated")
        for dim, d in health["breakdown"].items():
            pct = d["score"] / d["max"] if d["max"] else 0
            st.progress(pct, text=f"{dim}: {d['score']:.0f} / {d['max']}")
            with st.expander(f"How is {dim} scored?"):
                st.caption(HEALTH_EXPLANATIONS.get(dim, ""))
                if d["findings"]:
                    for f in d["findings"]:
                        st.markdown(f"- {f}")
                else:
                    st.markdown("- No issues found — full marks available.")
        st.markdown('</div>', unsafe_allow_html=True)

    domains = [DOMAIN_LABELS.get(d, d) for d in detect_candidate_domains(profile)]
    edu = (profile.get("education") or [{}])[0]
    theme.card(f"""
<b>{profile.get('name', 'Candidate')}</b><br>
<span class="uja-muted">{profile.get('email', '')} · {profile.get('phone', '')}</span><br><br>
<b>Education:</b> {edu.get('degree', '—')}, {edu.get('college', '—')}
({edu.get('years', '—')}{', CGPA ' + edu['cgpa'] if edu.get('cgpa') else ''})<br>
<b>Projects:</b> {len(profile.get('projects', []))} ·
<b>Skills:</b> {len(profile.get('skills', []))}<br><br>
<b>Detected domains:</b><br>{theme.pills(domains or ['None detected'])}
""")


# ---- Step 5: skills ---------------------------------------------------------

def render_skills(profile: dict) -> None:
    skills = profile.get("skills", [])
    if not skills:
        st.info("No skills detected.")
        return
    by_category: dict[str, list[str]] = {}
    for s in skills:
        by_category.setdefault(s.get("category", "Other"), []).append(s["skill"])

    cols = st.columns(2)
    for i, (category, names) in enumerate(sorted(by_category.items())):
        with cols[i % 2]:
            theme.card(f"<b>{category}</b><br><br>{theme.pills(sorted(names))}")


# ---- Step 6: skill gaps -----------------------------------------------------

def render_gaps(gaps: list[dict], careers: list[dict]) -> None:
    target = ", ".join(c["role"] for c in careers[:3])
    st.markdown(
        f'<p class="uja-muted">Weighted by demand within your target roles '
        f'({target}) — a skill in 12% of all jobs can appear in 60% of the '
        'jobs that matter to you. All numbers computed from your collected '
        'job corpus.</p>', unsafe_allow_html=True)
    if not gaps:
        st.success("No significant gaps against the current job corpus.")
        return
    cols = st.columns(3)
    for i, g in enumerate(gaps):
        variant = {"High": "red", "Medium": "amber", "Low": "gray"}[g["priority"]]
        # Headline the role-weighted number only when the subset is big
        # enough to trust; otherwise headline overall demand.
        if g.get("role_weighted"):
            headline, unit = g["role_demand_pct"], "of your target-role jobs"
            sub = f"({g['demand_pct']}% overall · {g['jobs_mentioning']:,} postings)"
        else:
            headline, unit = g["demand_pct"], "of all collected jobs"
            sub = f"({g['jobs_mentioning']:,} postings)"
        with cols[i % 3]:
            theme.card(f"""
<b>{g['skill']}</b> <span class="uja-pill {variant}">{g['priority']}</span><br>
<span class="uja-metric" style="font-size:1.8rem;">{headline}%</span><br>
<span class="uja-muted">{unit}<br>{sub}</span>
""")


# ---- Step 7: suggestions ----------------------------------------------------

def render_suggestions(suggestions: list[dict]) -> None:
    if not suggestions:
        st.success("No major issues found.")
        return
    for s in suggestions:
        theme.card(f"""
{theme.severity_pill(s['severity'])}
<span class="uja-pill gray">{s['category']}</span><br>
<b>{s['title']}</b><br>
<span class="uja-muted">{s['detail']}</span>
""")


# ---- Step 8: career fit -----------------------------------------------------

def render_careers(careers: list[dict]) -> None:
    if not careers:
        st.info("Not enough signal to recommend roles.")
        return
    for c in careers:
        st.markdown('<div class="uja-card">', unsafe_allow_html=True)
        left, right = st.columns([3, 1])
        with left:
            st.markdown(f"**{c['role']}**")
        with right:
            st.markdown(f'<div class="uja-metric" style="font-size:1.9rem;'
                        f'text-align:right;">{c["fit_pct"]}%</div>',
                        unsafe_allow_html=True)
        st.progress(c["fit_pct"] / 100)
        st.markdown("✓ You have: " + theme.pills(c["evidence"][:8], "green"),
                    unsafe_allow_html=True)
        if c.get("missing"):
            st.markdown("To strengthen: " + theme.pills(c["missing"][:5], "gray"),
                        unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)


# ---- caching wrappers -------------------------------------------------------

@st.cache_data(show_spinner=False)
def cached_analysis(profile: dict, jobs_mtime: float,
                    _jobs: list[dict]) -> dict:
    """All analysis in one cached call, keyed on profile + jobs file mtime.

    Careers are computed FIRST so skill gaps can be weighted by the
    candidate's actual target roles.
    """
    health = compute_health(profile)
    careers = recommend_careers(profile)
    target_roles = [c["role"] for c in careers[:3]]
    gaps = compute_skill_gaps(profile, _jobs, target_roles=target_roles)
    return {
        "health": health,
        "careers": careers,
        "gaps": gaps,
        "suggestions": generate_suggestions(profile, health, gaps),
    }
