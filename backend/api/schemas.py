"""Pydantic schemas -- the API's public contract, decoupled from internal
dict shapes so engines can evolve without breaking clients."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ---- profile -----------------------------------------------------------------

class Skill(BaseModel):
    skill: str
    category: str = "Other"
    domain: str = "General"


class Education(BaseModel):
    college: str = ""
    degree: str = ""
    cgpa: str = ""
    years: str = ""


class Project(BaseModel):
    title: str
    duration: str = ""
    description: str = ""


class Profile(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    linkedin: Optional[str] = None
    github: Optional[str] = None
    skills: list[Skill] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)


class ParseResult(BaseModel):
    profile: Profile
    parse_confidence: dict[str, Any]


# ---- analysis ----------------------------------------------------------------

class HealthDimension(BaseModel):
    score: float
    max: int
    findings: list[str]


class HealthResponse(BaseModel):
    score: float
    grade: str
    breakdown: dict[str, HealthDimension]


class SkillGap(BaseModel):
    skill: str
    demand_pct: float
    role_demand_pct: float
    jobs_mentioning: int
    role_jobs: int
    role_weighted: bool
    priority: str


class CareerFit(BaseModel):
    role: str
    fit_pct: int
    evidence: list[str]
    missing: list[str]


class Suggestion(BaseModel):
    title: str
    detail: str
    severity: str
    category: str


# ---- rankings ----------------------------------------------------------------

class RankingStatus(BaseModel):
    state: str                     # idle | running | done | failed
    detail: str = ""
    ranked_count: Optional[int] = None


class RankedJob(BaseModel):
    title: Optional[str]
    company: Optional[str]
    location: Optional[str] = None
    match_score: float
    score_breakdown: dict[str, float]
    strong_matches: list[str] = Field(default_factory=list)
    likely_matches: list[Any] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    apply_link: Optional[str] = None


# ---- tracker -----------------------------------------------------------------

class EventCreate(BaseModel):
    company: str = Field(min_length=1)
    title: str = Field(min_length=1)
    event_type: str = Field(
        pattern="^(save|applied|interview|rejected|offer|dismiss|click)$")
    note: Optional[str] = None
    match_score: Optional[float] = None
    score_breakdown: Optional[dict[str, float]] = None


class TrackedEvent(BaseModel):
    event_type: str
    created_at: Any
    note: Optional[str] = None


class TrackedJob(BaseModel):
    company: str
    title: str
    status: str
    apply_link: Optional[str] = None
    match_score: Optional[float] = None
    last_activity: Any
    history: list[TrackedEvent]
