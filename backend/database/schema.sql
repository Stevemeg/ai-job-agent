-- Phase 2 schema: users, canonical_jobs, events.
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
