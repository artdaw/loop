#!/usr/bin/env bash
#
# Loop — one-command setup for a single user's instance.
#
# Verifies the host has Docker + Docker Compose, creates a personal .env from
# the template (prompting for the handful of secrets Loop needs), pulls the
# local Ollama models, and starts the whole stack with Docker Compose.
#
# Usage:
#   ./scripts/setup.sh
#
# Re-running is safe: an existing .env is kept (you are asked before it is
# overwritten) and Docker Compose only (re)creates what changed.

set -euo pipefail

# --- Resolve paths ---------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_ROOT}"

# --- Pretty output ---------------------------------------------------------
BOLD="$(tput bold 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"
info()  { printf '%s\n' "${BOLD}==>${RESET} $*"; }
warn()  { printf '%s\n' "${BOLD}[!]${RESET} $*" >&2; }
die()   { printf '%s\n' "${BOLD}[x]${RESET} $*" >&2; exit 1; }

# --- 1. Prerequisites ------------------------------------------------------
info "Checking prerequisites…"
command -v docker >/dev/null 2>&1 || die "Docker is not installed. See https://docs.docker.com/get-docker/"

if docker compose version >/dev/null 2>&1; then
  COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  die "Docker Compose is not available. Install Docker Desktop or the compose plugin."
fi

docker info >/dev/null 2>&1 || die "The Docker daemon is not running. Start Docker and retry."
info "Docker and Compose detected (${COMPOSE})."

# --- 2. Create .env --------------------------------------------------------
if [[ -f .env ]]; then
  read -r -p "A .env already exists. Overwrite it? [y/N] " reply
  if [[ "${reply}" =~ ^[Yy]$ ]]; then
    cp .env ".env.backup.$(date +%Y%m%d%H%M%S)"
    info "Backed up the existing .env."
    cp config/.env.example .env
  else
    info "Keeping the existing .env."
  fi
else
  cp config/.env.example .env
  info "Created .env from config/.env.example."
fi

# Set a key in .env in place (portable across GNU and BSD sed).
set_env() {
  local key="$1" value="$2"
  # Escape sed replacement metacharacters.
  local escaped
  escaped="$(printf '%s' "${value}" | sed -e 's/[&/\\]/\\&/g')"
  if grep -q "^${key}=" .env; then
    sed -i.bak "s|^${key}=.*|${key}=${escaped}|" .env && rm -f .env.bak
  else
    printf '%s=%s\n' "${key}" "${value}" >> .env
  fi
}

prompt_secret() {
  # prompt_secret KEY "Human description"
  local key="$1" desc="$2" current answer
  current="$(grep -E "^${key}=" .env | head -n1 | cut -d= -f2- || true)"
  printf '  %s (%s)\n' "${BOLD}${key}${RESET}" "${desc}"
  printf '    current: %s\n' "${current:-<unset>}"
  read -r -p "    new value [keep current]: " answer
  if [[ -n "${answer}" ]]; then
    set_env "${key}" "${answer}"
  fi
}

info "Let's fill in your secrets (press Enter to keep the current/placeholder value)."
prompt_secret TELEGRAM_BOT_TOKEN "from @BotFather"
prompt_secret TELEGRAM_CHAT_ID   "your Telegram chat id"
prompt_secret ANTHROPIC_API_KEY  "cloud fallback LLM; leave blank to stay fully local"
prompt_secret OBSIDIAN_VAULT_PATH "absolute path to your Obsidian vault"

# --- 3. Start the stack ----------------------------------------------------
info "Building the app with uv and starting the Loop stack (ollama + chromadb + app)…"
${COMPOSE} up -d --build

# --- 4. Pull Ollama models -------------------------------------------------
# The ollama-init service pulls models on first `up`, but we pull explicitly so
# setup blocks until the models are actually available.
info "Pulling local models (llama3.1:8b + nomic-embed-text) — this can take a while…"
${COMPOSE} exec -T ollama ollama pull llama3.1:8b || warn "Could not pull llama3.1:8b (is ollama healthy yet?)"
${COMPOSE} exec -T ollama ollama pull nomic-embed-text || warn "Could not pull nomic-embed-text"

info "${BOLD}Loop is up.${RESET}"
cat <<'EOF'

Next steps:
  • Web dashboard:   http://localhost:8000
  • CLI (in Docker): docker compose run --rm app loop status
  • Morning brief:   docker compose run --rm app loop briefing
  • Ask a question:  docker compose run --rm app loop ask "what's on today?"

Edit .env any time to add Gmail/Outlook/Teams/Wrike credentials, then re-run
`docker compose up -d` to apply the changes.
EOF
