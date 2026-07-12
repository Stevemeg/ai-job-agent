"""
api.py -- single clean entry point for resume parsing.

WHY: build_candidate_profile() in resume_parser.py hardcodes a relative
"../datasets/..." path that only works from inside backend/. This wrapper
resolves paths via backend.config so callers (Streamlit UI today, FastAPI
tomorrow) can parse a PDF from anywhere without knowing internals.
Existing extractor modules are reused untouched.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..config import SKILLS_DB, PROFILE_FILE
from .resume_parser import (
    extract_text_from_pdf,
    extract_links_from_pdf,
    classify_links,
)
from .text_cleaner import clean_resume_text
from .skill_extractor import load_skills_database, extract_skills
from .entity_extractor import extract_name, extract_email, extract_phone
from .education_extractor import extract_education
from .project_extractor import extract_projects


def parse_resume_pdf(pdf_path: str | Path) -> dict:
    """Parse a resume PDF into a candidate profile dict. Pure -- no writes."""
    raw_text = extract_text_from_pdf(str(pdf_path))
    if raw_text.startswith("Error:"):
        raise ValueError(f"Could not read PDF: {raw_text}")

    cleaned = clean_resume_text(raw_text)
    linkedin, github = classify_links(extract_links_from_pdf(str(pdf_path)))
    skills_df = load_skills_database(str(SKILLS_DB))

    return {
        "name": extract_name(cleaned),
        "email": extract_email(cleaned),
        "phone": extract_phone(cleaned),
        "linkedin": linkedin,
        "github": github,
        "skills": extract_skills(cleaned, skills_df),
        "education": extract_education(cleaned),
        "projects": extract_projects(cleaned),
    }


def save_profile(profile: dict, path: str | Path | None = None) -> Path:
    """Persist a profile, backing up any existing one to <name>.bak first.

    Default path resolves at CALL time (not import time) so tests can
    monkeypatch PROFILE_FILE on this module without touching real data."""
    path = Path(path) if path is not None else Path(PROFILE_FILE)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(profile, f, ensure_ascii=False, indent=4)
    return path
