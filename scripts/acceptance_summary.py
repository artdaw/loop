#!/usr/bin/env python3
"""Recompute the acceptance-matrix summary from its own rows.

The expected scenario count is read from `specification/acceptance.md`, so a
spec amendment that adds IDs fails loudly here instead of silently leaving the
matrix short.

Run after changing any row's status:

    uv run python scripts/acceptance_summary.py

Hand-maintaining the totals drifts from reality the moment a row changes, and a
wrong total is worse than none: it reads as coverage that does not exist.
"""

from __future__ import annotations

import pathlib
import re
import sys
from collections import defaultdict

MATRIX = pathlib.Path(__file__).resolve().parent.parent / "docs" / "ACCEPTANCE_MATRIX.md"
STATUSES = ("pending", "implemented", "verified", "blocked", "optional-unconfigured")
ROW = re.compile(r"^\| ([A-Z]{1,3}\d{2,3}) \|.*\| (" + "|".join(STATUSES) + r") \|$")
# The stage cell can name more than one stage ("C/E"), so this is a character
# class with a quantifier. A single-character pattern silently skipped that row
# and left its counts frozen at whatever was last written by hand.
SUMMARY = re.compile(r"^\| ([A-Z/—]+) \| (.+?) \| (\d+) \| \d+ \| \d+ \| \d+ \|$")


def main() -> int:
    text = MATRIX.read_text()
    section: str | None = None
    tally: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ids: set[str] = set()

    for line in text.splitlines():
        if line.startswith("## ") and line[3].isdigit():
            section = line[3:].strip()
        match = ROW.match(line)
        if match and section:
            tally[section][match.group(2)] += 1
            ids.add(match.group(1))

    # The count comes from the specification, not a constant here, so a spec
    # amendment (1.2 → 1.3 added LG01–LG12) cannot silently drift from the matrix.
    spec = MATRIX.parent / "specification" / "acceptance.md"
    expected = len({m.group(1) for m in
                    re.finditer(r"^\| *([A-Z]{1,3}\d{2,3}) *\|", spec.read_text(),
                                re.MULTILINE)})
    if len(ids) != expected:
        print(f"ERROR: matrix has {len(ids)} scenario IDs, acceptance.md has "
              f"{expected}", file=sys.stderr)
        return 1

    out = []
    for line in text.splitlines():
        summary = SUMMARY.match(line)
        if summary and summary.group(1) != "—":
            stage, label, total = summary.group(1), summary.group(2), int(summary.group(3))
            key = next((k for k in tally if k.startswith(label.split(".")[0] + ".")), None)
            counts: dict[str, int] = tally.get(key or "", {})
            verified = counts.get("verified", 0)
            implemented = counts.get("implemented", 0)
            out.append(f"| {stage} | {label} | {total} | {verified} | "
                       f"{implemented} | {total - verified - implemented} |")
        elif line.startswith("| — | **Total**"):
            v = sum(t.get("verified", 0) for t in tally.values())
            i = sum(t.get("implemented", 0) for t in tally.values())
            out.append(f"| — | **Total** | **{expected}** | **{v}** | **{i}** | "
                       f"**{expected - v - i}** |")
        else:
            out.append(line)

    MATRIX.write_text("\n".join(out) + "\n")
    verified = sum(t.get("verified", 0) for t in tally.values())
    implemented = sum(t.get("implemented", 0) for t in tally.values())
    print(f"{expected} scenarios: {verified} verified, {implemented} implemented, "
          f"{expected - verified - implemented} pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
