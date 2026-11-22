# Configuration

All configuration is via Streamlit Secrets (deployment) or environment variables (local/CLI/Docker). **Nothing is ever hardcoded.**

## LLM Provider (automatic selection)

The engines call `backend.llm.generate(prompt)` and never know which backend serves it:

| Condition | Provider | Default model |
|---|---|---|
| `GROQ_API_KEY` present (Streamlit Secrets or env) | **Groq** (hosted — production) | `llama-3.3-70b-versatile` |
| No key | **Ollama** (localhost — development) | `mistral` |

The deterministic hallucination validator runs on the output of *either* provider — no provider is trusted.

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
| `DB_USER` / `DB_PASSWORD` / `DB_NAME` / `DB_HOST` / `DB_PORT` | env / `.env` | dev defaults | PostgreSQL connection |
| `DATABASE_URL` | env | — | Full PostgreSQL URL (Render/Neon); takes priority over `DB_*` |
| `DB_CONNECT_TIMEOUT` | env | `3` | Seconds the corpus loader waits when probing the database |

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
