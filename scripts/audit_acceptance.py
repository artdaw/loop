#!/usr/bin/env python3
"""Audit the acceptance matrix against evidence that actually exists.

A green suite is not coverage. This checks the parts of that claim which can be
established mechanically, and — since R0 — it does so per *test function*
rather than per file.

**Why the file-level version was not good enough.** The first version decided a
test's evidence level from tokens anywhere in its file: a single `subprocess`
import made every row pointing into that file look like it proved a real
restart. That is a hint about a file, not evidence about a scenario. This
version parses each named test with `ast`, resolves the fixtures it actually
requests, and reports what *that function* reaches. A row can no longer inherit
rigour from a neighbour.

Four checks:

1. **Every scenario ID in the specification appears exactly once.** A dropped
   or duplicated row silently changes the denominator.
2. **Every named test exists and is collected.** Asked of pytest itself.
   Renamed or deleted tests cannot leave a row looking verified.
3. **Evidence level per test.** Derived from the call graph of the test and its
   fixtures: a real subprocess, a shipped interface, the composition root, or
   components.
4. **A `verified` row's evidence reaches what its required result demands.**
   A scenario needing a restart, a delivery or a shipped command cannot rest on
   a unit test.

**What this cannot do, stated so nobody mistakes it for more.** It cannot tell
whether a test at the right level asserts the right *substance*. That is a
reading task and stays one. These checks narrow what must be read; the
`--check` gate failing means something is definitely wrong, not that passing
means everything is right.

    uv run python scripts/audit_acceptance.py            # report
    uv run python scripts/audit_acceptance.py --check    # fail on any finding
    uv run python scripts/audit_acceptance.py --ids D14 A01   # explain rows
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "ACCEPTANCE_MATRIX.md"
SPEC = ROOT / "docs" / "specification" / "acceptance.md"
TESTS = ROOT / "tests"

ROW = re.compile(r"^\|\s*([A-Z]{1,3}\d{2,3})\s*\|")
SPEC_ID = re.compile(r"^\| *([A-Z]{1,3}\d{2,3}) *\|", re.MULTILINE)
TESTREF = re.compile(r"([\w./]+\.py)::([\w*]+)")

LEVELS = {"component": 0, "application": 1, "interface": 2, "process": 3}
NAMES = {value: key for key, value in LEVELS.items()}

#: Names whose appearance inside a test (or a fixture it uses) shows the level
#: that test actually runs at. Matched against attribute and call names in the
#: function's own AST, never against the file.
MARKERS: tuple[tuple[int, frozenset[str]], ...] = (
    (LEVELS["process"], frozenset({"run", "Popen", "check_output", "check_call"})),
    (LEVELS["interface"], frozenset({"CliRunner", "TestClient", "LoopTelegramBot",
                                     "invoke", "_handle_update"})),
    (LEVELS["application"], frozenset({"build_application"})),
)
#: `subprocess.run` and `runner.invoke` share bare names with ordinary calls, so
#: the process/interface markers only count when the module or object matches.
PROCESS_OWNERS = frozenset({"subprocess"})
INTERFACE_OWNERS = frozenset({"runner", "client", "api", "ui", "cli_runner"})

#: Phrases in a required result that demand more than a component test.
DEMANDS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (LEVELS["process"], ("restart", "reboot", "kill", "crash and")),
    (LEVELS["interface"], ("installed", "docker", "cli ", "command", "api ",
                           "http", "telegram", "bot ", "endpoint")),
    (LEVELS["application"], ("deliver", "notification", "outbox", "reply",
                             "message", "sent", "send", "acknowledgement",
                             "briefing")),
)

#: A required result that *forbids* what it names is satisfied by a component
#: test proving it did not happen.
NEGATIONS = ("no ", "not ", "never ", "without ", "cannot ", "refus",
             "suppress", "prevent", "blocked", "denied")


@dataclass
class Finding:
    kind: str
    identifier: str
    detail: str


@dataclass
class Report:
    rows: int = 0
    findings: list[Finding] = field(default_factory=list)
    levels: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    per_row: dict[str, tuple[int, int]] = field(default_factory=dict)

    def add(self, kind: str, identifier: str, detail: str) -> None:
        self.findings.append(Finding(kind, identifier, detail))


# --------------------------------------------------------------------------- #
# Per-test evidence, from the AST
# --------------------------------------------------------------------------- #
class _CallNames(ast.NodeVisitor):
    """Collects called names, attribute owners and referenced identifiers."""

    def __init__(self) -> None:
        self.calls: set[tuple[str, str]] = set()      # (owner, name)
        self.names: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:     # noqa: N802
        function = node.func
        if isinstance(function, ast.Attribute):
            owner = (function.value.id if isinstance(function.value, ast.Name)
                     else "")
            self.calls.add((owner, function.attr))
        elif isinstance(function, ast.Name):
            self.calls.add(("", function.id))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:     # noqa: N802
        self.names.add(node.id)
        self.generic_visit(node)


def _level_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    collector = _CallNames()
    collector.visit(node)
    level = LEVELS["component"]

    for owner, name in collector.calls:
        if name in MARKERS[0][1] and owner in PROCESS_OWNERS:
            return LEVELS["process"]
        if name in MARKERS[1][1] and (owner in INTERFACE_OWNERS or owner == ""):
            level = max(level, LEVELS["interface"])
        if name in MARKERS[2][1]:
            level = max(level, LEVELS["application"])
    for name in collector.names:
        if name in MARKERS[1][1]:
            level = max(level, LEVELS["interface"])
        if name in MARKERS[2][1]:
            level = max(level, LEVELS["application"])
    return level


def _module_levels(path: Path) -> dict[str, int]:
    """Evidence level for every test in a file, resolving its fixtures.

    A test that requests a fixture inherits what that fixture reaches: a
    `client` fixture building a `TestClient` makes its callers interface-level,
    which is true, while the file-wide scan would also have credited every
    unrelated unit test in the same file.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return {}

    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions[node.name] = node

    own = {name: _level_of(node) for name, node in functions.items()}
    called: dict[str, set[str]] = {}
    for name, node in functions.items():
        collector = _CallNames()
        collector.visit(node)
        called[name] = {call for _owner, call in collector.calls
                        if call in functions}

    resolved: dict[str, int] = {}

    def resolve(name: str, seen: frozenset[str]) -> int:
        """What this test reaches, through its fixtures *and* its helpers.

        Both edges are followed because both are how a test really gets there:
        a `client` fixture building a `TestClient` makes its callers
        interface-level, and a `_run_cli` helper wrapping `subprocess.run`
        makes them process-level. Following neither was the file-level scan's
        problem in reverse — it credited every test in the file instead of the
        ones that actually reach it.
        """
        if name in resolved:
            return resolved[name]
        node = functions.get(name)
        if node is None or name in seen:
            return LEVELS["component"]
        resolved[name] = own[name]          # guards against helper cycles
        level = own[name]
        for argument in node.args.args:
            if argument.arg in functions:
                level = max(level, resolve(argument.arg, seen | {name}))
        for helper in called[name]:
            level = max(level, resolve(helper, seen | {name}))
        resolved[name] = level
        return level

    return {name: resolve(name, frozenset()) for name in functions}


# --------------------------------------------------------------------------- #
# Matrix and collection
# --------------------------------------------------------------------------- #
def collect_node_ids() -> dict[str, set[str]]:
    """Ask pytest what it collects. Nothing is inferred from file names."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(TESTS), "--collect-only",
         "-p", "no:randomly", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=ROOT)
    by_file: dict[str, set[str]] = defaultdict(set)
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" not in line:
            continue
        path, _, rest = line.partition("::")
        by_file[Path(path).name].add(rest.split("[")[0])
    return by_file


def demanded_level(required: str) -> int:
    lowered = required.lower()
    for level, phrases in DEMANDS:
        for phrase in phrases:
            if phrase not in lowered:
                continue
            index = lowered.index(phrase)
            window = lowered[max(0, index - 40):index + len(phrase)]
            if any(negation in window for negation in NEGATIONS):
                continue
            return level
    return LEVELS["component"]


def audit() -> Report:
    report = Report()
    collected = collect_node_ids()
    module_levels = {path.name: _module_levels(path)
                     for path in TESTS.rglob("test_*.py")}

    spec_ids = set(SPEC_ID.findall(SPEC.read_text())) if SPEC.is_file() else set()
    seen: list[str] = []

    for line in MATRIX.read_text().splitlines():
        match = ROW.match(line)
        if not match:
            continue
        report.rows += 1
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        identifier, required, status = cells[0], cells[2], cells[-1]
        seen.append(identifier)
        refs = TESTREF.findall(cells[-2])

        if not refs:
            report.add("dangling", identifier, "names no test")
            continue

        level = LEVELS["component"]
        for filename, pattern in refs:
            name = Path(filename).name
            names = collected.get(name)
            if names is None:
                report.add("dangling", identifier,
                           f"{filename} is not collected")
                continue
            matched = [n for n in names if fnmatch.fnmatch(n, pattern)]
            if not matched:
                report.add("dangling", identifier,
                           f"nothing matches {filename}::{pattern}")
                continue
            levels = module_levels.get(name, {})
            level = max([level, *(levels.get(n, 0) for n in matched)])

        want = demanded_level(required)
        report.levels[NAMES[level]] += 1
        report.per_row[identifier] = (level, want)
        if want > level and status == "verified":
            report.add(
                "shortfall", identifier,
                f"have {NAMES[level]}, want {NAMES[want]} — {required[:90]}")

    duplicates = {i for i in seen if seen.count(i) > 1}
    for identifier in sorted(duplicates):
        report.add("duplicate", identifier, "appears more than once")
    if spec_ids:
        for identifier in sorted(spec_ids - set(seen)):
            report.add("missing", identifier, "in the specification, not the matrix")
        for identifier in sorted(set(seen) - spec_ids):
            report.add("unknown", identifier, "in the matrix, not the specification")
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero on any finding")
    parser.add_argument("--ids", nargs="*", default=[],
                        help="explain these rows and exit")
    arguments = parser.parse_args()

    report = audit()

    if arguments.ids:
        for identifier in arguments.ids:
            entry = report.per_row.get(identifier)
            if entry is None:
                print(f"{identifier}: not in the matrix")
                continue
            have, want = entry
            print(f"{identifier}: evidence {NAMES[have]}, demanded {NAMES[want]}")
        return 0

    print(f"rows: {report.rows}")
    print("evidence level available (per named test, not per file):")
    for name in ("process", "interface", "application", "component"):
        print(f"  {name:<12} {report.levels[name]:>4}")
    print()

    by_kind: dict[str, list[Finding]] = defaultdict(list)
    for finding in report.findings:
        by_kind[finding.kind].append(finding)

    for kind in ("missing", "unknown", "duplicate", "dangling", "shortfall"):
        entries = by_kind.get(kind)
        if not entries:
            continue
        print(f"{kind}: {len(entries)}")
        for finding in entries:
            print(f"  {finding.identifier}: {finding.detail}")
        print()

    if not report.findings:
        print("Every specification ID appears once, names a collected test, and "
              "no `verified` row rests on evidence below what it demands.")

    return 1 if (arguments.check and report.findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
