# Universal AI Job Agent -- shared image for the API and UI services.
# Both services need the same engines; docker-compose.yml picks the command.
#
# Build context notes: .dockerignore excludes venv/, node_modules/, data/
# (mounted as a volume so rankings/profiles persist outside containers).

FROM python:3.11-slim

# psycopg2-binary and PyMuPDF ship manylinux wheels; no build toolchain needed.
# curl is kept for container healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Layer-cache dependencies separately from source.
COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY backend/ backend/
COPY datasets/ datasets/
COPY .streamlit/ .streamlit/

ENV PYTHONUNBUFFERED=1 \
    # Embedding model cache lives in a named volume (see compose) so the
    # ~90 MB all-MiniLM-L6-v2 download happens once, not per container.
    HF_HOME=/models

EXPOSE 8000 8501

# Default command is the API; compose overrides for the UI service.
CMD ["uvicorn", "backend.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
