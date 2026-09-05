#!/usr/bin/env python3
"""Recompute the acceptance-matrix summary from its own rows.

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
SUMMARY = re.compile(r"^\| ([A-Z—]) \| (.+?) \| (\d+) \| \d+ \| \d+ \| \d+ \|$")


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

    if len(ids) != 184:
        print(f"ERROR: found {len(ids)} scenario IDs, expected 184", file=sys.stderr)
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
            out.append(f"| — | **Total** | **184** | **{v}** | **{i}** | **{184 - v - i}** |")
        else:
            out.append(line)

    MATRIX.write_text("\n".join(out) + "\n")
    verified = sum(t.get("verified", 0) for t in tally.values())
    print(f"184 scenarios: {verified} verified, "
          f"{sum(t.get('implemented', 0) for t in tally.values())} implemented, "
          f"{184 - verified - sum(t.get('implemented', 0) for t in tally.values())} pending")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
