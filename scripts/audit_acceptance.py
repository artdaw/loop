#!/usr/bin/env python3
"""Re-audit the acceptance matrix against tests that actually exist and run.

The plan is explicit that a green suite is not coverage: *"a row is `verified`
only when its named test asserts the scenario's required result"*, and that the
inherited labels require re-audit. Two of those three things can be checked
mechanically, and this checks them on every release run so the answer cannot
quietly drift again:

1. **Does the named test exist?** A row pointing at a test that was renamed or
   never written cannot be verified whatever its label says. This is exact.
2. **Does it run at the level the required result demands?** Plan §4: "Unit
   tests remain useful but cannot alone verify a scenario requiring an
   interface, restart, provider or delivery path." A row whose result mentions
   a restart, a delivered message or a shipped command, but whose only test is
   a component unit test, is flagged.

The third — whether an existing test at the right level actually asserts the
required *substance* — is a reading task and stays a human one. This script
narrows what has to be read; it does not pretend to replace it.

**On the second check's false positives.** Many required results mention
delivery only to forbid it: "never silently assigned to owner or messaged
externally", "no send without that scope's authority". A component test
asserting that nothing was sent is exactly right for those, so a negated
demand is not counted as a shortfall. That rule is why the flagged set is
small enough to read.

    uv run python scripts/audit_acceptance.py            # report
    uv run python scripts/audit_acceptance.py --check    # fail on a shortfall
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "ACCEPTANCE_MATRIX.md"
TESTS = ROOT / "tests"

ROW = re.compile(r"^\|\s*([A-Z]{1,3}\d{2,3})\s*\|")
TESTREF = re.compile(r"([\w./]+\.py)::([\w*]+)")

LEVELS = {"component": 0, "application": 1, "interface": 2, "process": 3}
NAMES = {value: key for key, value in LEVELS.items()}

#: What a test file's own content says about the level it runs at.
FILE_SIGNALS = (
    (LEVELS["process"], ("subprocess",)),
    (LEVELS["interface"], ("CliRunner", "TestClient", "LoopTelegramBot")),
    (LEVELS["application"], ("build_application",)),
)

#: Phrases in a required result that demand more than a component test.
DEMANDS = (
    (LEVELS["process"], ("restart", "reboot", "kill", "crash and")),
    (LEVELS["interface"], ("installed", "docker", "cli ", "command", "api ",
                           "http", "telegram", "bot ", "endpoint")),
    (LEVELS["application"], ("deliver", "notification", "outbox", "reply",
                             "message", "sent", "send", "acknowledgement",
                             "briefing")),
)

#: A required result that *forbids* the thing it names is satisfied by a
#: component test proving it did not happen.
NEGATIONS = ("no ", "not ", "never ", "without ", "cannot ", "refus",
             "suppress", "prevent", "blocked", "denied")


def collect_node_ids() -> dict[str, set[str]]:
    """Ask pytest what it actually collects. Nothing is inferred from names."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS), "--collect-only",
         "-p", "no:randomly", "-p", "no:cacheprovider", "-q"],
        capture_output=True, text=True, cwd=ROOT)
    by_file: dict[str, set[str]] = defaultdict(set)
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        path, _, rest = line.partition("::")
        by_file[Path(path).name].add(rest.split("[")[0])
    if not by_file:
        # `-q` prints per-file counts on some pytest versions; fall back to the
        # verbose node-id listing rather than reporting an empty, all-clear run.
        result = subprocess.run(
            [sys.executable, "-m", "pytest", str(TESTS), "--collect-only",
             "-p", "no:randomly", "-p", "no:cacheprovider"],
            capture_output=True, text=True, cwd=ROOT)
        for line in result.stdout.splitlines():
            line = line.strip()
            if "::" not in line:
                continue
            path, _, rest = line.partition("::")
            by_file[Path(path).name].add(rest.split("[")[0])
    return by_file


def file_level(path: Path) -> int:
    body = path.read_text(encoding="utf-8", errors="replace")
    for level, tokens in FILE_SIGNALS:
        if any(token in body for token in tokens):
            return level
    return LEVELS["component"]


def demanded_level(required: str) -> int:
    lowered = required.lower()
    for level, phrases in DEMANDS:
        for phrase in phrases:
            if phrase not in lowered:
                continue
            window = lowered[max(0, lowered.index(phrase) - 40):
                             lowered.index(phrase) + len(phrase)]
            if any(negation in window for negation in NEGATIONS):
                continue
            return level
    return LEVELS["component"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero on a dangling reference or on a "
                             "`verified` row whose evidence falls short")
    arguments = parser.parse_args()

    collected = collect_node_ids()
    levels = {path.name: file_level(path) for path in TESTS.rglob("test_*.py")}

    dangling: list[str] = []
    shortfall: list[tuple[str, int, int, str]] = []
    have_counts: dict[str, int] = defaultdict(int)
    rows = 0

    for line in MATRIX.read_text().splitlines():
        match = ROW.match(line)
        if not match:
            continue
        rows += 1
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        identifier, required, status = cells[0], cells[2], cells[-1]
        refs = TESTREF.findall(cells[-2])

        if not refs:
            dangling.append(f"{identifier}: names no test")
            continue

        files: set[str] = set()
        for filename, pattern in refs:
            name = Path(filename).name
            names = collected.get(name)
            if names is None:
                dangling.append(f"{identifier}: {filename} is not collected")
                continue
            if not any(fnmatch.fnmatch(n, pattern) for n in names):
                dangling.append(
                    f"{identifier}: nothing matches {filename}::{pattern}")
                continue
            files.add(name)

        have = max((levels.get(f, 0) for f in files), default=0)
        want = demanded_level(required)
        have_counts[NAMES[have]] += 1
        if want > have and status == "verified":
            shortfall.append((identifier, have, want, required[:110]))

    print(f"rows: {rows}")
    print("evidence level available:")
    for name in ("process", "interface", "application", "component"):
        print(f"  {name:<12} {have_counts[name]:>4}")
    print()

    if dangling:
        print(f"dangling test references: {len(dangling)}")
        for entry in dangling:
            print(f"  {entry}")
        print()

    if shortfall:
        print(f"`verified` rows whose evidence falls short: {len(shortfall)}")
        for identifier, have, want, required in shortfall:
            print(f"  {identifier}: have {NAMES[have]}, want {NAMES[want]}")
            print(f"      {required}")
        print()

    if not dangling and not shortfall:
        print("Every row names a collected test, and no `verified` row rests "
              "on evidence below what its required result demands.")

    if arguments.check and (dangling or shortfall):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
