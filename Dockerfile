# Loop — the assistant app image.
FROM python:3.12-slim

# Avoid interactive prompts and keep Python output unbuffered.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Minimal build deps (some wheels need a compiler / git at build time).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && pip install .

# Copy the application source.
COPY . .

# Local runtime data (SQLite + ChromaDB) — mounted as a volume in compose.
RUN mkdir -p /app/data

# Default: run the orchestrator daemon. Override for the CLI or web UI.
#   docker compose run --rm app loop status
#   uvicorn web.main:app --host 0.0.0.0 --port 8000
CMD ["python", "-m", "cli.main", "status"]
