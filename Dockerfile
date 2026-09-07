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

# Run as a dedicated unprivileged user. A container that runs as root shares
# that root with the host through any bind mount, so a bug that writes outside
# /app writes as uid 0 on the host too.
RUN useradd --create-home --uid 10001 loop

# Local runtime data (SQLite + ChromaDB) — mounted as a volume in compose.
# Created before the USER switch so it can be chowned to the run user.
RUN mkdir -p /app/data && chown -R loop:loop /app

USER loop

# Default: run the durable vNext scheduler and workers. Override for CLI/API.
#   docker compose run --rm app loop-next status
#   uvicorn web.main:app --host 0.0.0.0 --port 8000
CMD ["loop-next", "run", "daemon"]
