#!/usr/bin/env bash
# A reproducible walkthrough of the shipped commands (M7).
#
# Every step runs `loop-next` — the installed entry point, not a Python import
# — against a throwaway directory, and checks an observable outcome. So it is
# a demo you can read and a smoke test that fails loudly, rather than a
# transcript someone pasted once and that has been wrong ever since.
#
# Hermetic by construction: a temporary vault, a temporary database, and no
# credentials. Steps that would need a network or an account are *named and
# skipped* rather than quietly omitted — an unconfigured provider is a fact
# about this machine, not a gap in the demo.
#
#   scripts/demo.sh            # run it
#   KEEP=1 scripts/demo.sh     # keep the directory afterwards to poke around
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
LOOP="${LOOP:-$REPO/.venv/bin/loop-next}"

[ -x "$LOOP" ] || { echo "no loop-next at $LOOP; run: uv sync" >&2; exit 1; }

ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loop-demo.XXXXXX")"
# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() { [ -n "${KEEP:-}" ] || rm -rf "$ROOT"; }
trap cleanup EXIT

VAULT="$ROOT/vault"
mkdir -p "$VAULT/0-raw/inbox" "$VAULT/1-wiki/concepts" "$VAULT/_ctx/loop"
printf '| source | batch | added | status | compiled | pages produced |\n|---|---|---|---|---|---|\n' \
  > "$VAULT/0-raw/_ledger.md"

export DATABASE_URL="sqlite:///$ROOT/loop.db"
export DATA_DIR="$ROOT/data"
export OBSIDIAN_VAULT_PATH="$VAULT"
export CAPABILITY_PATHS="[\"$REPO/packs\"]"
export TELEGRAM_CHAT_ID=demo TELEGRAM_USER_ID=demo

pass=0
step()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
check() { # check <description> <expected substring> <actual>
  if printf '%s' "$3" | grep -qF -- "$2"; then
    printf '   ok  %s\n' "$1"; pass=$((pass + 1))
  else
    printf '   FAIL %s\n     wanted: %s\n     got: %s\n' "$1" "$2" "$3" >&2
    exit 1
  fi
}
skip()  { printf '   skipped  %s\n     reason: %s\n' "$1" "$2"; }

step "1. It starts with nothing configured"
out="$($LOOP status)"
check "status runs with no model and no packs enabled" "triggers enabled: 0" "$out"
check "it names its own limitations rather than implying none" "note:" "$out"

step "2. A commitment, and the reminder that carries it"
out="$($LOOP task add 'call the repair shop')"
task_id="$(printf '%s' "$out" | awk '{print $1}')"
check "the task is persisted and given an id" "call the repair shop" "$out"
out="$($LOOP task list)"
check "it is listed back" "$task_id" "$out"

step "3. Knowledge: exact capture, then a cited answer"
out="$($LOOP vault capture 'Supplier lead time is six weeks | confirmed by email')"
check "the reply states what actually happened" "Saved to your vault: 0-raw/inbox/" "$out"
raw="${out#Saved to your vault: }"
check "the body is preserved byte for byte, pipe and all" \
      'six weeks | confirmed by email' "$(cat "$VAULT/$raw")"
check "the ledger registered it as pending" "pending" "$(cat "$VAULT/0-raw/_ledger.md")"

out="$($LOOP vault reindex)"
check "the vault is indexed for retrieval" "document(s) indexed" "$out"
out="$($LOOP vault ask 'supplier lead time')"
check "an uncompiled capture is labelled as such, not presented as knowledge" \
      "uncompiled" "$out"
out="$($LOOP vault ask 'quarterly revenue in Patagonia')"
check "nothing in the vault is a stated gap, not an invented answer" \
      "nothing in the vault" "$out"

if [ -n "${OLLAMA_DEFAULT_MODEL:-}" ]; then
  step "3b. Compiling a capture into a sourced page"
  out="$($LOOP vault compile || true)"
  printf '   %s\n' "$out"
else
  skip "vault compile" "no OLLAMA_DEFAULT_MODEL; claim extraction needs a model"
fi

step "4. Capabilities: two packs, and one route to both"
out="$($LOOP capability list)"
check "plantcare is discovered from packs/" "plantcare" "$out"
check "bikeservice is discovered too" "bikeservice" "$out"
$LOOP capability enable plantcare 1.0.0 >/dev/null
out="$($LOOP status)"
check "enabling a pack needs no new command or code" "plantcare" "$out"

step "5. Routines: saving is not activating"
cat > "$ROOT/rain.md" <<'YAML'
---
schema_version: 1
id: rain-check
title: Rain check
trigger:
  kind: local_schedule
  days: [mon, tue, wed, thu, fri, sat, sun]
  at: "07:00"
  timezone: Europe/Berlin
steps:
  - capability: weather.prepare
    arguments:
      location_ref: home
notification:
  mode: each_occurrence
  destination: "demo"
---

Every morning.
YAML
out="$($LOOP routine add "$ROOT/rain.md")"
check "the routine is saved as proposed" "rain-check saved (proposed)" "$out"
out="$($LOOP routine list)"
check "a saved routine is not scheduled" "unscheduled" "$out"
out="$($LOOP routine activate rain-check --event-id demo-request)"
check "activation names the request that authorised it" "rain-check active" "$out"
check "and only then does it have a next run" "next run" "$out"
out="$($LOOP status)"
check "the schedule is durable, visible in status" "triggers enabled: 1" "$out"
out="$($LOOP routine pause rain-check)"
check "pausing stops it firing" "rain-check paused" "$out"

step "6. Learning proposes; it never applies"
$LOOP learning snooze routine:rain-check 30 --event-id demo-1 >/dev/null
$LOOP learning snooze routine:rain-check 30 --event-id demo-2 >/dev/null
out="$($LOOP learning review)"
check "two snoozes are below the learner's threshold, and it says so" \
      "no proposal" "$out"
out="$($LOOP learning snooze routine:rain-check 30 --event-id demo-1)"
check "the same event cannot be counted twice" "already recorded" "$out"

step "7. The sweep is deterministic and reaches no model"
out="$($LOOP run once --sweep-only)"
check "it reports what it did" "sweep(s)" "$out"

step "8. Backup and restore are one verified pair"
out="$($LOOP backup --output "$ROOT/backup")"
printf '   %s\n' "$out"
check "a backup is written" "backup written to" "$out"
# The command prints a part count; the manifest is what says *what* was
# captured, and a backup missing the vault would still print "written".
manifest="$(cat "$ROOT/backup/loop-backup.json")"
check "the manifest names the domain database" '"name": "database"' "$manifest"
check "and the vault alongside it" '"name": "vault"' "$manifest"
check "each part carries a checksum to verify against" '"sha256"' "$manifest"
# Verify-only first: restore refuses to write until it is asked to, and the
# target must be empty. A restore that silently overwrote a live directory is
# the one mistake this pairing exists to prevent.
out="$($LOOP restore --from "$ROOT/backup" --target "$ROOT/restored")"
check "restore verifies the backup before writing anything" "verified" "$out"
check "and only says what it *would* do until asked" "would restore into" "$out"
if [ -e "$ROOT/restored" ]; then
  echo "   FAIL a verify-only restore created its target" >&2
  exit 1
fi
printf '   ok  the target is untouched by a verify-only run\n'; pass=$((pass + 1))
out="$($LOOP restore --from "$ROOT/backup" --target "$ROOT/restored" --apply)"
check "applying into an explicit empty target succeeds" "restored" "$out"

step "Not demonstrated on this machine"
skip "weather forecast and briefing delivery" \
     "needs network access to Open-Meteo and a location in the vault manifest"
skip "official weather warnings" "needs a warning feed configured in the vault manifest"
skip "travel fares, availability and schedules" "no provider ships; each needs an account"
skip "Telegram delivery" "needs TELEGRAM_BOT_TOKEN and a paired chat"

printf '\n\033[1m%d checks passed.\033[0m\n' "$pass"
[ -n "${KEEP:-}" ] && printf 'kept: %s\n' "$ROOT"
exit 0
