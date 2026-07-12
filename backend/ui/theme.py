"""
theme.py -- design system for the Streamlit app.

One place for palette, typography, and reusable rendering components
(cards, pills, score rings). Views compose these; they never write raw
CSS themselves. Palette is a restrained SaaS look: near-black text,
one accent, generous whitespace.
"""
from __future__ import annotations

import streamlit as st

ACCENT = "#5B5BD6"        # indigo
ACCENT_SOFT = "#EEEEFB"
GREEN = "#30A46C"
AMBER = "#F5A623"
RED = "#E5484D"
TEXT = "#1A1A2E"
MUTED = "#6E6E85"
CARD_BG = "#FFFFFF"
PAGE_BG = "#F7F7FB"
BORDER = "#E4E4EF"

SEVERITY_COLORS = {"high": RED, "medium": AMBER, "low": MUTED}
PRIORITY_COLORS = {"High": RED, "Medium": AMBER, "Low": MUTED}

_GLOBAL_CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');

/* Palette/background comes from .streamlit/config.toml (base = light) so
   native widgets match. CSS here only adds typography + custom components.

   IMPORTANT: the font override must NEVER hit Streamlit's icon spans --
   icons are ligature words ("upload", "keyboard_arrow_down") in the
   Material Symbols font; overriding their font-family renders the literal
   word instead of the glyph (the "uploadpload" bug). */
html, body {{
    font-family: 'Inter', -apple-system, sans-serif;
}}
[data-testid="stMarkdownContainer"], .uja-card, .uja-hero {{
    font-family: 'Inter', -apple-system, sans-serif;
}}
span[data-testid="stIconMaterial"] {{
    font-family: 'Material Symbols Rounded' !important;
}}
#MainMenu, footer {{ visibility: hidden; }}
h1, h2, h3 {{ font-weight: 700; letter-spacing: -0.02em; }}

.uja-card {{
    background: {CARD_BG};
    border: 1px solid {BORDER};
    border-radius: 14px;
    padding: 1.1rem 1.3rem;
    margin-bottom: 0.9rem;
}}
.uja-pill {{
    display: inline-block;
    padding: 0.18rem 0.65rem;
    border-radius: 999px;
    font-size: 0.78rem;
    font-weight: 600;
    margin: 0.12rem 0.25rem 0.12rem 0;
    background: {ACCENT_SOFT};
    color: {ACCENT};
    border: 1px solid {BORDER};
}}
.uja-pill.green {{ background: #E9F7EF; color: {GREEN}; }}
.uja-pill.amber {{ background: #FEF6E7; color: #B07D10; }}
.uja-pill.red   {{ background: #FDEBEC; color: {RED}; }}
.uja-pill.gray  {{ background: #F1F1F6; color: {MUTED}; }}

.uja-muted {{ color: {MUTED}; font-size: 0.86rem; }}
.uja-metric {{ font-size: 2.6rem; font-weight: 800; line-height: 1; }}
.uja-hero {{
    text-align: center;
    padding: 3.5rem 1rem 1.5rem 1rem;
}}
.uja-hero h1 {{ font-size: 2.6rem; margin-bottom: 0.4rem; }}
.uja-hero p  {{ color: {MUTED}; font-size: 1.05rem; }}

.uja-score-ring {{
    width: 150px; height: 150px; border-radius: 50%;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    margin: 0 auto;
    color: {TEXT};
}}
</style>
"""


def inject_theme() -> None:
    st.markdown(_GLOBAL_CSS, unsafe_allow_html=True)


def card(html_body: str) -> None:
    st.markdown(f'<div class="uja-card">{html_body}</div>',
                unsafe_allow_html=True)


def pills(items: list[str], variant: str = "") -> str:
    cls = f"uja-pill {variant}".strip()
    return " ".join(f'<span class="{cls}">{i}</span>' for i in items)


def score_ring(score: float, grade: str) -> None:
    """Conic-gradient ring: green >=85, amber >=70, red below."""
    color = GREEN if score >= 85 else AMBER if score >= 70 else RED
    deg = score / 100 * 360
    st.markdown(f"""
<div class="uja-score-ring"
     style="background:
        radial-gradient(closest-side, {CARD_BG} 82%, transparent 83% 100%),
        conic-gradient({color} {deg}deg, {BORDER} 0deg);">
    <div style="font-size:2.2rem;font-weight:800;">{score:.0f}</div>
    <div style="font-size:0.85rem;color:{MUTED};">Grade {grade}</div>
</div>""", unsafe_allow_html=True)


def severity_pill(severity: str) -> str:
    variant = {"high": "red", "medium": "amber", "low": "gray"}[severity]
    return f'<span class="uja-pill {variant}">{severity.upper()}</span>'
