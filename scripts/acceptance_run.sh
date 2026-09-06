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
"$PY" -m ruff check loop tests

step "types"
"$PY" -m mypy loop

step "hermetic test suite"
"$PY" -m pytest tests/ -q --no-header

step "acceptance matrix totals"
"$PY" scripts/acceptance_summary.py

step "packaging, wheel and image checks"
LOOP_PACKAGING_TESTS=1 "$PY" -m pytest tests/vnext/test_packaging.py -q --no-header

step "restart durability"
"$PY" -m pytest tests/vnext/test_run_durability.py -q --no-header

printf '\nAll acceptance checks passed.\n'
