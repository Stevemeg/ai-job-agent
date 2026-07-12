# Security Policy

## Reporting a Vulnerability

Email **konabharath2004@gmail.com** with a description and reproduction steps. Please do not open public issues for security problems. You can expect an acknowledgment within a few days.

## Scope Notes

- **Local development is local-first**: resume data, profiles, and generated documents stay on the user's machine, and Ollama inference means resume content never leaves it. **The deployed Groq path is different**: resume/JD content is sent to Groq's hosted API for document generation — this trade-off is documented in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and must be disclosed in any public deployment.
- **API keys are never committed**: `GROQ_API_KEY` is read exclusively from Streamlit Secrets or environment variables (`backend/llm/provider.py`); `.streamlit/secrets.toml` and `.env` are gitignored; `.dockerignore` keeps secrets and personal data out of images; keys are never logged.
- Sensitive paths: `data/`, `uploads/` (personal data — gitignored), `backend/database/.env` (DB credentials — never commit; use `.env.example` as the template).
- The Streamlit UI has **no authentication** and must not be exposed to the public internet as-is (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)).
- Job collectors call public ATS board APIs read-only; no credentials are involved.
