#!/usr/bin/env bash
#
# Loop — provision an isolated instance for a new peer/user.
#
# Every user gets their OWN checkout, .env, and Docker Compose project so their
# data (SQLite DB, ChromaDB vectors, Obsidian vault mount, credentials) is fully
# isolated from everyone else's. This clones Loop into a per-user directory and
# runs the standard setup there.
#
# Usage:
#   ./scripts/new_user.sh <username> [git-remote-url] [target-parent-dir]
#
# Examples:
#   ./scripts/new_user.sh alice
#   ./scripts/new_user.sh bob https://github.com/artdaw/loop.git ~/loop-instances
#
# If no git remote is given, the script copies the current checkout instead of
# cloning (handy for air-gapped or private setups).

set -euo pipefail

BOLD="$(tput bold 2>/dev/null || true)"
RESET="$(tput sgr0 2>/dev/null || true)"
info() { printf '%s\n' "${BOLD}==>${RESET} $*"; }
die()  { printf '%s\n' "${BOLD}[x]${RESET} $*" >&2; exit 1; }

USERNAME="${1:-}"
GIT_REMOTE="${2:-}"
PARENT_DIR="${3:-$HOME/loop-instances}"

[[ -n "${USERNAME}" ]] || die "Usage: $0 <username> [git-remote-url] [target-parent-dir]"
# Sanitise the username to a safe directory/project slug.
SLUG="$(printf '%s' "${USERNAME}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' | sed 's/^-//; s/-$//')"
[[ -n "${SLUG}" ]] || die "Username '${USERNAME}' has no usable characters."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_DIR="${PARENT_DIR}/loop-${SLUG}"

mkdir -p "${PARENT_DIR}"
[[ ! -e "${TARGET_DIR}" ]] || die "Target ${TARGET_DIR} already exists. Remove it or pick another username."

# --- 1. Obtain the code ----------------------------------------------------
if [[ -n "${GIT_REMOTE}" ]]; then
  info "Cloning ${GIT_REMOTE} into ${TARGET_DIR}…"
  git clone --depth=1 "${GIT_REMOTE}" "${TARGET_DIR}"
else
  info "Copying the current checkout into ${TARGET_DIR}…"
  mkdir -p "${TARGET_DIR}"
  # Copy tracked files only when possible, otherwise fall back to a filtered cp.
  if git -C "${SOURCE_ROOT}" rev-parse >/dev/null 2>&1; then
    git -C "${SOURCE_ROOT}" archive HEAD | tar -x -C "${TARGET_DIR}"
  else
    cp -R "${SOURCE_ROOT}/." "${TARGET_DIR}/"
    rm -rf "${TARGET_DIR}/.git" "${TARGET_DIR}/.venv" "${TARGET_DIR}/data"
  fi
fi

# --- 2. Isolate the Compose project ---------------------------------------
# A per-user COMPOSE_PROJECT_NAME keeps containers, networks and volumes
# separate, so instances never share state or clobber each other's ports.
cd "${TARGET_DIR}"
if [[ ! -f .env ]]; then
  cp config/.env.example .env
fi
if grep -q '^COMPOSE_PROJECT_NAME=' .env; then
  sed -i.bak "s|^COMPOSE_PROJECT_NAME=.*|COMPOSE_PROJECT_NAME=loop-${SLUG}|" .env && rm -f .env.bak
else
  printf 'COMPOSE_PROJECT_NAME=loop-%s\n' "${SLUG}" >> .env
fi
info "Configured isolated Compose project: loop-${SLUG}"

# --- 3. Run the standard per-user setup -----------------------------------
info "Handing off to scripts/setup.sh for ${USERNAME}…"
chmod +x scripts/setup.sh
./scripts/setup.sh

info "${BOLD}Instance for ${USERNAME} is ready at ${TARGET_DIR}.${RESET}"
