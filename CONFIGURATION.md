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

## Why `llama-3.3-70b-versatile`

Groq's **production-tier** list (preview models "may be discontinued at short notice" per Groq's deprecation policy). 70B-class instruction following matters here: the tailoring prompts demand strict JSON plus hard constraints ("do NOT add tools not in the original bullets") — smaller models are cheaper but violate constraints more often, and the validator would simply reject more of their output. It also supports `response_format: json_object`, eliminating markdown-fence parsing failures. Verified against [Groq's model docs](https://console.groq.com/docs/models) at implementation time; re-check that page before changing `GROQ_MODEL`.
