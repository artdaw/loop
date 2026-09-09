#!/usr/bin/env bash
# The full acceptance run (O14).
#
# Everything a release is gated on, in the order that fails cheapest first:
# lint and types before tests, tests before builds, builds before the image.
# A failure stops the run — a partially green run reported as green is worse
# than a red one.
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PY:-.venv/bin/python}"

step() { printf '\n=== %s ===\n' "$1"; }

step "lint"
"$PY" -m ruff check .

step "types"
"$PY" -m mypy . --exclude '^build/'

step "hermetic test suite"
"$PY" -m pytest tests/ -q --no-header

step "acceptance matrix totals"
"$PY" scripts/acceptance_summary.py --check

step "acceptance matrix audit"
# Totals alone cannot catch a row pointing at a test that no longer exists, or
# a `verified` label resting on a unit test where the scenario needs a restart
# or a delivery path. This is the check that makes "no mandatory behaviour
# falsely marked complete" a gate rather than a claim.
"$PY" scripts/audit_acceptance.py --check

step "source and wheel build"
uv build

step "packaging and installed-wheel checks (Docker excluded)"
LOOP_PACKAGING_TESTS=1 "$PY" -m pytest tests/vnext/test_packaging.py \
  -k 'not o03' -q --no-header

step "restart durability"
"$PY" -m pytest tests/vnext/test_run_durability.py \
  tests/vnext/test_release_e2e.py -q --no-header

step "reproducible demo through the installed commands"
# The demo drives `loop-next` itself, so it fails when a shipped command's
# behaviour drifts from what the walkthrough claims — which a transcript in a
# document cannot do.
bash scripts/demo.sh

step "working-tree integrity"
git diff --check

# O03 is intentionally last. Nothing after this line is a release gate: an
# unavailable daemon must fail the release rather than turn six skipped image
# checks into a green result.
step "Docker image checks (final release gate)"
if ! docker info >/dev/null 2>&1; then
  printf 'Docker daemon is unavailable; release is not verified.\n' >&2
  exit 1
fi
LOOP_PACKAGING_TESTS=1 "$PY" -m pytest tests/vnext/test_packaging.py \
  -k o03 -q --no-header

printf '\nAll acceptance checks, including Docker, passed.\n'
