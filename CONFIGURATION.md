# Configuration

All configuration is via Streamlit Secrets (deployment) or environment variables (local/CLI/Docker). **Nothing is ever hardcoded.**

## LLM Provider

The engines call `backend.llm.generate(prompt)` and never know which backend serves it. `LLM_PROVIDER` picks one; unset (or `auto`) keeps the automatic selection:

| `LLM_PROVIDER` | Condition | Provider | Default model |
|---|---|---|---|
| `auto` (default) | `GROQ_API_KEY` present (Streamlit Secrets or env) | **Groq** (hosted — production) | `llama-3.3-70b-versatile` |
| `auto` (default) | No key | **Ollama** (localhost — development) | `mistral` |
| `groq` | Needs `GROQ_API_KEY` (error without it) | **Groq** | `llama-3.3-70b-versatile` |
| `ollama` | Ignores any `GROQ_API_KEY` | **Ollama** | `mistral` |
| `fake` | Always | **Fake**: deterministic and offline, for CI and tests (`backend/llm/fake_provider.py`) | — |

The deterministic hallucination validator runs on the output of *every* provider — no provider is trusted.

The fake provider files each call under a step (`extract_requirements`, `tailor_resume`, `tailor_bullets`, `order_skills`, `cover_letter`) by a marker phrase in its prompt and answers with a deterministic function of the prompt. Tests steer it with `FAKE_LLM_SLEEP` (`step=seconds,...`), `FAKE_LLM_RAISE` (`step=unavailable|error|none,...`) and `FAKE_LLM_LOG` (a file that gets one JSON line per call, written before any sleep, so calls are countable across processes). CI runs with `LLM_PROVIDER=fake`.

### Setting the Groq key

**Streamlit Cloud:** Manage app → Settings → Secrets:

```toml
GROQ_API_KEY = "gsk_..."
```

**Local file** (gitignored): `.streamlit/secrets.toml`, same content.

**Environment** (CLI, API, Docker): `GROQ_API_KEY=gsk_...` (Docker: put it in the root `.env`; compose passes it to the UI container).

If the key is missing AND Ollama isn't running, document generation shows a friendly in-app message explaining both options — it never crashes.

## All Settings

| Variable | Where | Default | Purpose |
|---|---|---|---|
| `GROQ_API_KEY` | Secrets / env | — | Enables Groq hosted inference |
| `GROQ_MODEL` | Secrets / env | `llama-3.3-70b-versatile` | Override Groq model (production-tier IDs only) |
| `GROQ_TIMEOUT` | env | `120` | Groq request timeout (s) |
| `OLLAMA_TIMEOUT` | env | `480` | Local Ollama timeout (s); first call loads the model |
| `OLLAMA_MODEL` | env | `mistral` | Override the local Ollama model (any model you have pulled, e.g. `qwen2.5:1.5b-instruct-fp16`) |
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` / `DB_HOST` / `DB_PORT` | env / `.env` | dev defaults | PostgreSQL connection |
| `DATABASE_URL` | env | — | Full PostgreSQL URL (Render/Neon); takes priority over `DB_*` |
| `DB_CONNECT_TIMEOUT` | env | `3` | Seconds the corpus loader waits when probing the database |
| `SCAN_CRON` | env / `.env` | `0 9 * * *` | When the scheduler queues a scan: a 5-field crontab in **Asia/Kolkata** time (the default is 09:00 IST = 03:30 UTC) |
| `SCAN_MISFIRE_GRACE_S` | env / `.env` | `3600` | How late a missed scan may still run, in seconds. If the scheduler was down at the fire time and comes back within this window, the slot runs once; after that it is skipped. Several missed slots run once |
| `LLM_PROVIDER` | env | `auto` | `auto`, `groq`, `ollama` or `fake` (see above) |
| `AGENT_ENABLED` | env / `.env` (worker) | `false` | When `true`, every scan queues an agent run as its last step. Manual runs (`POST /v1/agent/runs`) work either way |
| `AGENT_MIN_SCORE` | env / `.env` (worker) | `80` | Triage: a job's `match_score` must be at least this |
| `AGENT_MAX_JOB_AGE_DAYS` | env / `.env` (worker) | `14` | Triage: the job must have been seen by a scan within this many days (`last_seen_at`) |
| `AGENT_MAX_DRAFTS_PER_RUN` | env / `.env` (worker) | `5` | Triage: at most this many new applications per run (`0` drafts nothing) |
| `AGENT_LOCATIONS` | env / `.env` (worker) | `Bengaluru, Bangalore, Hyderabad, Pune, Chennai, Mumbai, Gurgaon, Gurugram, Noida, Delhi, NCR, India` | Triage: comma-separated. A job's location, or one of its postings', must contain one of these as whole words (case and spacing ignored) |
| `AGENT_ACCEPT_GLOBAL_REMOTE` | env / `.env` (worker) | `false` | Triage: `true` also accepts remote jobs that don't mention India (plain "Remote", "Remote - USA"). Remote jobs that mention India are always accepted |
| `AGENT_APPROVAL_TTL_DAYS` | env / `.env` (worker) | `7` | An application nobody approves, edits or declines within this many days of its latest draft expires |
| `GMAIL_CREDENTIALS_FILE` | env / `.env` (worker) | — | Path of a Google OAuth **Desktop app** client JSON. Gmail drafts are on only when this is set |
| `GMAIL_TOKEN_FILE` | env / `.env` (worker) | `data/secrets/gmail_token.json` | Where the OAuth token is written and read (gitignored) |
| `API_TOKEN` | env / `.env` (API) | — | When set, every mutating API endpoint needs `Authorization: Bearer <API_TOKEN>` (401 otherwise). Unset: no check, so never expose the API beyond localhost without it |

## Scheduled scans

`python -m backend.scheduler` reads `SCAN_CRON` and `SCAN_MISFIRE_GRACE_S` at start-up and refuses to start (exit code `4`) if either is malformed. Changing `SCAN_CRON` replaces the stored schedule on the next start, and the next run is computed from the new expression. The scheduler needs the database (`DATABASE_URL` or `DB_HOST`), and so does the worker that runs the scans. See [DEPLOYMENT.md](docs/DEPLOYMENT.md#scan-scheduler).

```bash
SCAN_CRON="30 6 * * 1-5" SCAN_MISFIRE_GRACE_S=1800 DB_HOST=localhost python -m backend.scheduler   # 06:30 IST on weekdays
```

## The agent (Phase 4, dry run)

The agent drafts a tailored resume and cover letter for the best new jobs. It never sends, uploads or submits anything (ADR-001). The worker reads the `AGENT_*` variables at the start of each run, so a changed `.env` takes effect on the next run without a restart. A malformed value fails the run with a clear error.

Triage takes jobs that were scored for the **current** profile under the **current** scoring cache key, passed the hard gates, score at least `AGENT_MIN_SCORE`, were seen within `AGENT_MAX_JOB_AGE_DAYS`, have no application yet, have no `applied`, `dismiss` or `declined` event, and are in a location the agent may draft for. It takes the best `AGENT_MAX_DRAFTS_PER_RUN` of them.

Location rules (triage only; scoring and ranking never look at them): a location qualifies when it contains one of `AGENT_LOCATIONS` as whole words (`"Bengaluru, India"` contains `bengaluru`; `"Indianapolis"` doesn't contain `india`). A remote location (`remote`, `anywhere`, `worldwide`, `wfh`) qualifies only if it mentions India, or with `AGENT_ACCEPT_GLOBAL_REMOTE=true`. A job qualifies if its own location or any of its postings' does. When a posting qualifies, the application uses that posting's location and `apply_link`.

Drafts go to `data/applications/<application_id>/`. See [DEPLOYMENT.md](docs/DEPLOYMENT.md#agent-dry-run) and [API.md](docs/API.md#agent-phase-4-dry-run).

```bash
AGENT_ENABLED=true AGENT_MIN_SCORE=75 DB_HOST=localhost python -m backend.worker
```

## Approvals and Gmail drafts (Phase 5)

Every drafted application waits for you (`awaiting_approval`) in the Streamlit **Inbox** tab or via `POST /v1/applications/{id}/decision` ([API.md](docs/API.md#approval-gate-phase-5)). Approving builds the application packet. Nothing is ever sent or submitted: you apply from the apply link. A version nobody decides on within `AGENT_APPROVAL_TTL_DAYS` (7) expires.

**Gmail drafts (optional).** With `GMAIL_CREDENTIALS_FILE` set, approving an application whose job description names a contact address also creates **one Gmail draft** to it, with the resume and cover letter attached. It is a draft: the agent cannot send it (the Gmail wrapper has no send code at all; the `gmail.compose` scope alone would allow it). To set it up:

1. In Google Cloud, create an OAuth client of type **Desktop app** with the Gmail API enabled, and download its JSON (e.g. to `data/secrets/gmail_client.json`; `data/secrets/` is gitignored).
2. Authorize once, on a machine with a browser: `GMAIL_CREDENTIALS_FILE=data/secrets/gmail_client.json python -m backend.integrations.gmail_auth`. This asks for the single `gmail.compose` scope and writes `data/secrets/gmail_token.json` (or `GMAIL_TOKEN_FILE`).
3. Give the worker the same `GMAIL_CREDENTIALS_FILE` (and `GMAIL_TOKEN_FILE` if you moved it). The token refreshes itself.

Without `GMAIL_CREDENTIALS_FILE`, approval stops at the packet (`ready_to_apply`).

## Job corpus source

Every reader (API, Streamlit, CLI) loads jobs through `backend/database/corpus.py`, in this order:

1. **PostgreSQL `canonical_jobs`**, but only if a database is **explicitly configured** (`DATABASE_URL` or `DB_HOST` is set), it is reachable (probed once per process with `DB_CONNECT_TIMEOUT`), and it holds collected jobs.
2. **`data/jobs.json`**, the full local corpus (gitignored).
3. **`data/sample_jobs.json`**, the committed 120-job sample used by deployments.

The tracker still uses the `DB_*` dev defaults with nothing set. The corpus deliberately doesn't, so a DB-less deployment never tries to connect. For local development against the Docker database, set `DB_HOST=localhost` to have the collector and the app use Postgres:

```powershell
$env:DB_HOST = "localhost"
python -m backend.job_scraper.ats_collector            # upserts into canonical_jobs
python -m backend.database.corpus import data/jobs.json # one-time: seed the DB from an existing JSON corpus
python -m backend.database.corpus export                # DB -> data/jobs.json (refreshes the JSON fallback)
python -m backend.database.corpus status                # which source is active, and its size
```

With no database, the collector dedupes into `data/jobs.json` on (company, normalized title) instead of appending.

## Why `llama-3.3-70b-versatile`

Groq's **production-tier** list (preview models "may be discontinued at short notice" per Groq's deprecation policy). 70B-class instruction following matters here: the tailoring prompts demand strict JSON plus hard constraints ("do NOT add tools not in the original bullets") — smaller models are cheaper but violate constraints more often, and the validator would simply reject more of their output. It also supports `response_format: json_object`, eliminating markdown-fence parsing failures. Verified against [Groq's model docs](https://console.groq.com/docs/models) at implementation time; re-check that page before changing `GROQ_MODEL`.
