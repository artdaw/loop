# Loop — the assistant app image.
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.9 /uv /uvx /usr/local/bin/

# Avoid interactive prompts and keep Python output unbuffered.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_NO_CACHE=1

# Minimal build deps (some wheels need a compiler / git at build time).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the package sources before uv builds and installs the project.
COPY . .
RUN uv pip install --system .

# Local runtime data (SQLite + ChromaDB) — mounted as a volume in compose.
RUN mkdir -p /app/data

# Default: run the orchestrator daemon. Override for the CLI or web UI.
#   docker compose run --rm app loop status
#   uvicorn web.main:app --host 0.0.0.0 --port 8000
CMD ["loop", "telegram"]
