"""Career-intelligence endpoints -- 1:1 over backend.analysis functions."""
from __future__ import annotations

from fastapi import APIRouter

from ...analysis.resume_health import compute_health
from ...analysis.skill_gap import compute_skill_gaps
from ...analysis.career_recommender import recommend_careers
from ...analysis.suggestions import generate_suggestions
from ..deps import load_profile, load_jobs
from ..schemas import CareerFit, HealthResponse, SkillGap, Suggestion

router = APIRouter(prefix="/profile", tags=["analysis"])


@router.get("/health", response_model=HealthResponse)
def health():
    return compute_health(load_profile())


@router.get("/careers", response_model=list[CareerFit])
def careers():
    return recommend_careers(load_profile())


@router.get("/gaps", response_model=list[SkillGap])
def gaps():
    profile = load_profile()
    target = [c["role"] for c in recommend_careers(profile)[:3]]
    return compute_skill_gaps(profile, load_jobs(), target_roles=target)


@router.get("/suggestions", response_model=list[Suggestion])
def suggestions():
    profile = load_profile()
    h = compute_health(profile)
    target = [c["role"] for c in recommend_careers(profile)[:3]]
    g = compute_skill_gaps(profile, load_jobs(), target_roles=target)
    return generate_suggestions(profile, h, g)
