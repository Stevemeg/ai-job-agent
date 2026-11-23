"""Pydantic schemas -- the API's public contract, decoupled from internal
dict shapes so engines can evolve without breaking clients."""
from __future__ import annotations

from datetime import datetime
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


# ---- scans --------------------------------------------------------------------

class ScanSourceFailure(BaseModel):
    source: str                    # greenhouse | lever | ashby
    slug: str
    error: str


class ScanResult(BaseModel):
    inserted: int                  # new canonical jobs
    updated: int                   # existing jobs sighted again
    changed: int                   # ...whose content changed
    scored: int                    # jobs (re)scored by this scan's rank
    skipped_unchanged: int         # jobs whose stored score was current
    sources_ok: int
    sources_failed: list[ScanSourceFailure] = Field(default_factory=list)
    duration_s: float
    corpus_version: Optional[str] = None
    profile_hash: Optional[str] = None      # None: no profile, rank skipped
    formula_version: str
    ranked_count: Optional[int] = None
    rank_skipped: Optional[str] = None


class ScanStatus(BaseModel):
    state: str                     # idle | running | done | failed
    detail: str = ""
    task_id: Optional[int] = None
    trigger: Optional[str] = None  # schedule | api
    scheduled_for: Optional[str] = None     # the slot, for scheduled scans
    attempts: int = 0
    max_attempts: Optional[int] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[ScanResult] = None     # set once state is done


# ---- agent (Phase 4, dry run) -----------------------------------------------

class DispatchedApplication(BaseModel):
    application_id: int
    task_id: int
    job_id: int
    match_score: float
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None          # the qualifying posting's, else the job's


class AgentRunResult(BaseModel):
    trigger: Optional[str] = None           # api | scan
    scan_task_id: Optional[int] = None
    formula_version: str
    skipped: Optional[str] = None           # set when there was nothing to do
    profile_hash: Optional[str] = None
    cache_key: Optional[str] = None         # scoring cache key the run triaged under
    refresh: Optional[dict[str, Any]] = None   # the incremental rank's stats
    settings: Optional[dict[str, Any]] = None  # the AGENT_* values used
    triaged: int = 0
    dispatched: list[DispatchedApplication] = Field(default_factory=list)
    skipped_existing: int = 0


class AgentRunStatus(BaseModel):
    state: str                     # idle | running | done | failed
    detail: str = ""
    task_id: Optional[int] = None
    trigger: Optional[str] = None
    scan_task_id: Optional[int] = None
    attempts: int = 0
    max_attempts: Optional[int] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    result: Optional[AgentRunResult] = None  # set once state is done


class ApplicationSummary(BaseModel):
    id: int
    job_id: int
    title: str
    company: str
    location: Optional[str] = None
    apply_link: Optional[str] = None
    # queued | drafting | awaiting_approval | approved | declined | expired |
    # ready_to_apply | email_drafted | failed
    status: str
    match_score: Optional[float] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    needs_review: bool = False     # a validator flagged the current documents
    version: int = 0               # documents version; decisions name it


class ApplicationArtifactInfo(BaseModel):
    kind: str                      # resume_docx | cover_letter_docx | cover_letter_txt | jd_requirements
    sha256: str
    size_bytes: Optional[int] = None        # None: the file is gone from disk
    created_at: datetime
    download_url: str
    report: Optional[Any] = None   # the validator's report for this file


class ApplicationTask(BaseModel):
    task_id: int
    state: str
    detail: str = ""
    attempts: int
    max_attempts: int


class ApplicationDetail(ApplicationSummary):
    profile_hash: str
    cache_key: str
    thread_id: str
    validator: dict[str, Any] = Field(default_factory=dict)  # {"resume": ..., "cover_letter": ...}
    artifacts: list[ApplicationArtifactInfo] = Field(default_factory=list)
    task: Optional[ApplicationTask] = None


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
