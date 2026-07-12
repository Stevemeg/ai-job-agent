"""Resume upload/parse + profile CRUD. Thin adapters only."""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from ..deps import load_profile
from ..schemas import ParseResult, Profile

router = APIRouter(tags=["profile"])


@router.post("/resumes", response_model=ParseResult)
async def upload_resume(file: UploadFile):
    """Parse a resume PDF. Saves the profile (backing up any existing one)
    and returns it with parse-confidence so clients can drive a review step."""
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(422, "Only PDF resumes are supported.")
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:            # audit fix: size cap
        raise HTTPException(413, "Resume PDF larger than 10 MB.")
    # Heavy import (PyMuPDF/pandas) deferred so the API boots without them.
    from ...resume_parser.api import parse_resume_pdf, save_profile
    from ...analysis.parse_confidence import compute_parse_confidence

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)
        profile = parse_resume_pdf(tmp_path)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)

    save_profile(profile)
    return {"profile": profile,
            "parse_confidence": compute_parse_confidence(profile)}


@router.get("/profile", response_model=Profile)
def get_profile():
    return load_profile()


@router.put("/profile", response_model=Profile)
def put_profile(profile: Profile):
    """Save a user-corrected profile (the API equivalent of Review & Edit)."""
    from ...resume_parser.api import save_profile
    data = profile.model_dump()
    save_profile(data)
    return data
