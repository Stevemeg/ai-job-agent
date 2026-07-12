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
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

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
                        'offer'         -- received an offer (manual)
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