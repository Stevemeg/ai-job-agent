-- Schema: users, canonical_jobs, events, tasks, job_scores, applications,
-- application_artifacts, approvals, application_actions, agent_memories.
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

    -- Who caused an agent event (Phase 5): 'user' (an approval decision
    -- from the UI/API), 'expiry' (an approval nobody answered within
    -- AGENT_APPROVAL_TTL_DAYS) or 'agent' (the agent itself, e.g.
    -- 'drafted'). NULL for v1 and manually logged events. Existing
    -- databases get it via db.py:_run_migrations().
    source          TEXT CONSTRAINT events_source_check
                        CHECK (source IN ('user', 'expiry', 'agent')),

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
-- the application graph (LangGraph thread thread_id) drafts it, then
-- waits for the user (Phase 5):
--   queued -> drafting -> awaiting_approval -> approved -> ready_to_apply
--                      |                    |            -> email_drafted
--                      |                    -> declined | expired
--                      -> failed (its drafting task died)
-- An edit re-drafts in place: still awaiting_approval, version + 1.
-- One per (user, job), ever. Existing databases get these tables from
-- db.py:init_db() (create_all); Phase 4 rows in 'drafted'/'needs_review'
-- are moved to awaiting_approval by db.py:_run_migrations().
CREATE TABLE IF NOT EXISTS applications (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id          INTEGER NOT NULL REFERENCES canonical_jobs(id) ON DELETE CASCADE,
    profile_hash    TEXT NOT NULL,                     -- the profile it was triaged for
    cache_key       TEXT NOT NULL,                     -- the job_scores cache key of its score
    match_score     NUMERIC(5,2),                      -- score at triage
    status          TEXT NOT NULL DEFAULT 'queued' CONSTRAINT applications_status_check
                        CHECK (status IN ('queued', 'drafting', 'awaiting_approval',
                                          'approved', 'declined', 'expired',
                                          'ready_to_apply', 'email_drafted',
                                          'failed')),
    thread_id       TEXT NOT NULL UNIQUE,              -- "application:<id>"
    error           TEXT,                              -- why it failed
    needs_review    BOOLEAN NOT NULL DEFAULT false,    -- a validator flagged the current documents
    -- The documents' version: 0 until the first draft, 1 for it, +1 per
    -- edit. A decision names the version it was made on (optimistic
    -- concurrency: a stale one is rejected with 409).
    version         INTEGER NOT NULL DEFAULT 0,
    -- The job posting triage matched on location ({source, posting_id,
    -- location, apply_link}); its location and apply_link are the
    -- application's. NULL: the job's own location matched. (Phase 5;
    -- Phase 4 databases get it via db.py:_run_migrations().)
    posting         JSONB,
    -- Which agent memories changed this job's place in triage (Phase 6):
    -- {"boost": n, "memories": [{id, kind, value, provenance, weight}]}.
    -- NULL: none did. Its match_score is never changed by a memory.
    memory_effects  JSONB,
    -- A follow-up is due (Phase 6, agent/followups.py): you logged 'applied'
    -- AGENT_FOLLOW_UP_DAYS ago and no later outcome. Logging one clears it.
    follow_up_due   BOOLEAN NOT NULL DEFAULT false,
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
                                        'cover_letter_txt', 'jd_requirements',
                                        'packet')),
    path            TEXT NOT NULL,                     -- on the worker's disk
    sha256          TEXT NOT NULL,
    report          JSONB,                             -- validator output, if any
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT application_artifacts_application_id_kind_key UNIQUE (application_id, kind)
);

-- v2 approval gate (Phase 5): one decision per (application, version).
-- POST /v1/applications/{id}/decision inserts approve / decline / edit
-- (source 'user') in the same transaction as the "application_decision"
-- task that resumes the graph; an "approval_expiry" task inserts 'expire'
-- (source 'expiry') when nobody decided in time. The UNIQUE key is what
-- makes a second decision on the same version a 409.
CREATE TABLE IF NOT EXISTS approvals (
    id                  SERIAL PRIMARY KEY,
    application_id      INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    decision            TEXT NOT NULL CONSTRAINT approvals_decision_check
                            CHECK (decision IN ('approve', 'decline', 'edit', 'expire')),
    edits               JSONB,                         -- the requested edits (decision 'edit')
    reason              TEXT,
    source              TEXT NOT NULL CONSTRAINT approvals_source_check
                            CHECK (source IN ('user', 'expiry')),
    application_version INTEGER NOT NULL,              -- the version decided on
    decided_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- An expiry is never a person's decision, and a person never "expires".
    CONSTRAINT approvals_expire_source_check
        CHECK ((decision = 'expire') = (source = 'expiry')),
    CONSTRAINT approvals_application_id_application_version_key
        UNIQUE (application_id, application_version)
);

-- What executing an approved application did, one row per (application,
-- action): 'packet' (the application packet the human applies with),
-- 'gmail_draft' (one Gmail draft, never sent) and, Phase 6,
-- 'gmail_follow_up' (one follow-up draft, never sent). A Gmail row is
-- inserted 'pending' with its deterministic Message-ID BEFORE Gmail is
-- called, so a retry finds an already-created draft by that Message-ID
-- instead of creating a second one.
CREATE TABLE IF NOT EXISTS application_actions (
    id              SERIAL PRIMARY KEY,
    application_id  INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    action          TEXT NOT NULL CONSTRAINT application_actions_action_check
                        CHECK (action IN ('packet', 'gmail_draft', 'gmail_follow_up')),
    status          TEXT NOT NULL DEFAULT 'pending' CONSTRAINT application_actions_status_check
                        CHECK (status IN ('pending', 'done', 'failed')),
    external_id     TEXT,                              -- the Gmail draft id
    message_id      TEXT,                              -- "<aja-<id>-<version>@ai-job-agent>", "<aja-followup-<id>@...>"
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT application_actions_application_id_action_key UNIQUE (application_id, action)
);

-- v2 agent memory (Phase 6, backend/agent/memory.py): what the agent has
-- been told or has learned about a user's preferences. TRIAGE ONLY: an
-- active *_avoid memory keeps matching jobs out of triage, an active
-- *_prefer memory moves matching jobs up the triage order by a bounded
-- boost; neither ever changes a score, the score gate or job_scores.
--   provenance 'stated'  from the user (API / Inbox), never overwritten by
--                        learning
--   provenance 'learned' derived from the user's approve/decline decisions
--                        (deterministic thresholds, no LLM); evidence holds
--                        the approvals and events behind it
-- A deactivated memory has no effect and is never re-learned. Existing
-- databases get this table from db.py:init_db() (create_all).
CREATE TABLE IF NOT EXISTS agent_memories (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL CONSTRAINT agent_memories_kind_check
                        CHECK (kind IN ('company_avoid', 'title_avoid', 'company_prefer',
                                        'title_prefer', 'location_prefer', 'note')),
    value           TEXT NOT NULL,                     -- a company, title word(s), location or note
    provenance      TEXT NOT NULL CONSTRAINT agent_memories_provenance_check
                        CHECK (provenance IN ('stated', 'learned')),
    evidence        JSONB NOT NULL DEFAULT '{}',       -- approvals / events behind it, decline reasons
    weight          NUMERIC NOT NULL DEFAULT 1,        -- *_prefer: boost points; *_avoid: the count seen
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One memory per (user, kind, value), case-insensitively.
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_memories_user_kind_value
    ON agent_memories (user_id, kind, lower(value));

-- Not in this file: APScheduler's job store table (apscheduler_jobs), which
-- the scheduler (python -m backend.scheduler) creates and owns, and
-- LangGraph's checkpoint tables (checkpoints, checkpoint_blobs,
-- checkpoint_writes, checkpoint_migrations), which PostgresSaver.setup()
-- creates and owns (backend/agent/checkpoint.py runs it).
