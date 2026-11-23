-- Schema: users, canonical_jobs, events, tasks, job_scores, applications,
-- application_artifacts.
--
-- WHY canonical_jobs exists as a real table (not just JSON):
-- events need to reference "this exact job" reliably. JSON list entries have
-- no stable identity -- if jobs.json gets regenerated, append-only earlier
-- duplicates could shift list positions. A DB row with a real primary key
-- and a UNIQUE constraint on (company, normalized_title) gives every job a
-- stable id that an event can point to permanently, and doubles as the
-- canonical-job dedup layer the original roadmap called for.

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS canonical_jobs (
    id                  SERIAL PRIMARY KEY,
    company             TEXT NOT NULL,
    title               TEXT NOT NULL,
    normalized_title    TEXT NOT NULL,   -- lowercased, whitespace-collapsed, for the uniqueness check
    location            TEXT,
    tags                TEXT[],          -- Postgres array type, maps cleanly to a Python list
    experience          TEXT,
    seniority           TEXT,
    clean_description   TEXT,
    apply_link          TEXT,
    source              TEXT,            -- "remoteok" | "ats" | etc, provenance
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),   -- set on insert only
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),   -- bumped on every sighting

    -- v2 identity + change detection (written by database/corpus.py;
    -- existing databases get these via db.py:_run_migrations()).
    -- Every ATS posting behind this job: [{source, posting_id, location,
    -- apply_link}], merged on upsert and deduped on (source, posting_id).
    -- The same title in two locations stays ONE row with two postings.
    postings            JSONB NOT NULL DEFAULT '[]',
    -- sha256(normalized title + description + location set). NULL = stub
    -- row from impression/outcome logging, never collected -> not corpus.
    content_hash        TEXT,
    content_changed_at  TIMESTAMPTZ,                          -- moves only when content_hash does

    UNIQUE (company, normalized_title)
);

-- Speeds up the most common lookup pattern: "has this job been seen before".
CREATE INDEX IF NOT EXISTS idx_canonical_jobs_company_title
    ON canonical_jobs (company, normalized_title);

CREATE TABLE IF NOT EXISTS events (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id          INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,

    event_type      TEXT NOT NULL CHECK (event_type IN (
                        'impression',   -- shown in a ranked list (auto-logged)
                        'click',        -- user opened/viewed job detail
                        'save',         -- user bookmarked it
                        'dismiss',      -- user explicitly passed on it
                        'applied',      -- user applied (manual)
                        'interview',    -- got an interview (manual)
                        'rejected',     -- rejected (manual)
                        'offer',        -- received an offer (manual)
                        -- v2 agent decisions (ADR-001): auto-logged when the
                        -- approval gate resolves, never entered by hand.
                        -- Existing databases get these via the constraint
                        -- rewrite in db.py:_run_migrations().
                        'approved',     -- user approved the agent's application packet
                        'declined',     -- user declined the agent's suggestion
                        'drafted'       -- agent created a Gmail draft (never sent)
                    )),

    -- Snapshot of the score AT THE TIME of the event. Scoring weights will
    -- change over time as you tune them -- without this snapshot, you can't
    -- later ask "did high-scored jobs actually convert better?" because
    -- you'd only have today's weights to look back with, not the ones that
    -- were active when the impression happened.
    match_score         NUMERIC(5,2),
    score_breakdown     JSONB,

    -- Free-text note attached from the tracker UI ("referred by X",
    -- "phone screen Friday"). Added after v0.4; existing databases get it
    -- via the idempotent migration in db.py:_run_migrations().
    note            TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_user_id ON events (user_id);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON events (job_id);
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events (event_type);

-- v2 durable task queue (ADR-001 §2), written and claimed by
-- backend/taskqueue (SELECT ... FOR UPDATE SKIP LOCKED) and run by the worker
-- (python -m backend.worker). Existing databases get this table from
-- db.py:init_db() (create_all) and its Phase 2 columns/index from
-- db.py:_run_migrations().
CREATE TABLE IF NOT EXISTS tasks (
    id                  BIGSERIAL PRIMARY KEY,
    kind                TEXT NOT NULL,                 -- e.g. "rank", "scan"
    payload             JSONB NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'queued' CHECK (status IN (
                            'queued', 'running', 'succeeded', 'failed', 'dead')),
    idempotency_key     TEXT UNIQUE,                   -- duplicate enqueue returns the existing task, never a second run
    concurrency_key     TEXT,                          -- at most one queued/running task per key (index below)
    attempts            INT NOT NULL DEFAULT 0,        -- incremented at claim
    max_attempts        INT NOT NULL DEFAULT 3,
    run_after           TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_expires_at    TIMESTAMPTZ,                   -- worker lease; expired = reclaimable
    locked_by           TEXT,                          -- worker id holding (or last holding) the lease
    last_error          TEXT,
    result              JSONB,                         -- handler result, set on success
    started_at          TIMESTAMPTZ,                   -- latest claim
    finished_at         TIMESTAMPTZ,                   -- succeeded / dead
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The claim query's access path: "queued and due, oldest first".
CREATE INDEX IF NOT EXISTS idx_tasks_status_run_after ON tasks (status, run_after);

-- One active (queued or running) task per concurrency_key: a second enqueue
-- with the same key fails while one is active (the API maps that to 409).
CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_concurrency_key_active
    ON tasks (concurrency_key) WHERE status IN ('queued', 'running');

-- v2 incremental ranking (Phase 3): one job's cached matcher output for one
-- candidate profile under one scoring regime, written by
-- backend/services/scoring.py while the rank lock is held. A job is
-- re-scored only when no row for (job_id, profile_hash, cache_key)
-- carries its current content_hash; gate failures are stored too
-- (passed = false) so they are not re-evaluated. Existing databases get
-- this table from db.py:init_db() (create_all), and a Phase 3 table's
-- formula_version column is renamed to cache_key by _run_migrations().
CREATE TABLE IF NOT EXISTS job_scores (
    job_id              INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    profile_hash        TEXT NOT NULL,                 -- sha256 of the profile's canonical JSON
    cache_key           TEXT NOT NULL,                 -- scoring.cache_key(): "<FORMULA_VERSION>:<fingerprint[:12]>"
    content_hash        TEXT NOT NULL,                 -- scoring.score_input_hash(): every field the matcher reads
    passed              BOOLEAN NOT NULL,              -- the hard gates
    match_score         NUMERIC,                       -- NULL when gated out
    score_breakdown     JSONB,                         -- {skill_overlap, semantic, role, seniority}
    explanation         JSONB,                         -- {strong_matches, likely_matches, missing_skills}
    scored_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, profile_hash, cache_key),
    CONSTRAINT job_scores_passed_has_score_check CHECK (
        NOT passed OR (match_score IS NOT NULL AND score_breakdown IS NOT NULL
                       AND explanation IS NOT NULL))
);

-- Every rank execution reads all rows of one (profile, cache key).
CREATE INDEX IF NOT EXISTS idx_job_scores_profile_formula
    ON job_scores (profile_hash, cache_key);

-- v2 agent (Phase 4, backend/agent): one row per job the agent triaged for
-- a user and drafts a tailored resume + cover letter for. The run graph's
-- dispatch inserts it in the same transaction as its "application" task;
-- the application graph (LangGraph thread thread_id) moves it to drafted,
-- needs_review (any validator flag) or failed (its task died). One per
-- (user, job), ever. Existing databases get these tables from
-- db.py:init_db() (create_all).
CREATE TABLE IF NOT EXISTS applications (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id          INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    profile_hash    TEXT NOT NULL,                     -- the profile it was triaged for
    cache_key       TEXT NOT NULL,                     -- the job_scores cache key of its score
    match_score     NUMERIC(5,2),                      -- score at triage
    status          TEXT NOT NULL DEFAULT 'queued' CONSTRAINT applications_status_check
                        CHECK (status IN ('queued', 'drafting', 'drafted',
                                          'needs_review', 'failed')),
    thread_id       TEXT NOT NULL UNIQUE,              -- "application:<id>"
    error           TEXT,                              -- why it failed
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT applications_user_id_job_id_key UNIQUE (user_id, job_id)
);

-- The files an application's graph rendered, with their sha256 and the
-- truthfulness validator's report. One row per (application, kind): a
-- retried render overwrites it.
CREATE TABLE IF NOT EXISTS application_artifacts (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CONSTRAINT application_artifacts_kind_check
                        CHECK (kind IN ('resume_docx', 'cover_letter_docx',
                                        'cover_letter_txt', 'jd_requirements')),
    path            TEXT NOT NULL,                     -- on the worker's disk
    sha256          TEXT NOT NULL,
    report          JSONB,                             -- validator output, if any
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT application_artifacts_application_id_kind_key UNIQUE (application_id, kind)
);

-- Not in this file: APScheduler's job store table (apscheduler_jobs), which
-- the scheduler (python -m backend.scheduler) creates and owns, and
-- LangGraph's checkpoint tables (checkpoints, checkpoint_blobs,
-- checkpoint_writes, checkpoint_migrations), which PostgresSaver.setup()
-- creates and owns (backend/agent/checkpoint.py runs it).
