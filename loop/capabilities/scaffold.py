"""Create a new capability pack from the shipped template (EX01).

The extension contract's promise is that a new domain needs "no Coordinator,
scheduler, API or Telegram edits" — but that promise only holds if there is a
way to *start* a pack that already has the right shape. Until now the template
lived in `docs/specification/capabilities/template/` as something to copy by
hand, which meant the first thing every author did was rename identifiers in
five files and hope they caught them all.

Two properties this module exists to guarantee:

* **An explicit path, never a guessed one.** The output directory is required.
  Scaffolding into a default location is how a pack ends up somewhere the
  owner did not choose and does not find again.
* **It never overwrites.** If any target file already exists the whole
  operation is refused before a single byte is written, so re-running the
  command on a pack someone has been editing cannot silently discard their
  instructions. Partial success would be worse than either outcome: half a
  pack does not validate, and the author has still lost their work.

The generated pack is deliberately inert — `enabled: false`, no tools, no
destinations, one local artifact effect. Enabling is the owner's decision and
`CapabilityRegistry` refuses a manifest that tries to make it for them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from loop.core.errors import Conflict, InvalidInput

#: The template ships *inside* the package, so `capability init` works from an
#: installed wheel where `docs/` does not exist. `docs/specification/…/template`
#: remains the contract copy; `test_capability_scaffold.py` compares the two
#: byte for byte, so the duplication cannot quietly diverge.
TEMPLATE_ROOT = Path(__file__).resolve().parent / "template"

#: The pack id in the template, replaced with the caller's. Matching on the
#: full dotted id (not the bare word "example") keeps the substitution from
#: touching prose that happens to use the word.
TEMPLATE_ID = "example.checklist"

#: Files copied into a new pack. `README.md` is not among them: it documents
#: the template itself, and shipping it inside every new pack would leave a
#: file describing a scaffold command in a pack that has already been
#: scaffolded.
TEMPLATE_FILES = ("capability.yaml", "instructions.md",
                  "schemas/input.schema.json", "schemas/output.schema.json",
                  "examples/cases.yaml")

MODES = ("agent", "adapter", "workflow")

_ID = re.compile(r"^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)*$")


@dataclass(frozen=True)
class Scaffold:
    """What was created, so a caller can report it rather than guess."""

    package: Path
    files: tuple[Path, ...]
    pack_id: str
    version: str
    operation: str


def scaffold_pack(pack_id: str, *, output: Path, role: str = "daily_life",
                  mode: str = "agent", version: str = "1.0.0") -> Scaffold:
    """Write a new pack at `output`, refusing to overwrite anything.

    `output` is the package directory itself — the caller decides whether that
    is `./my-checklist` or a versioned path inside a capability root. Both are
    valid; discovery accepts a package directory either way.
    """
    if not _ID.match(pack_id):
        raise InvalidInput(
            f"{pack_id!r} is not a usable capability id. Use lowercase dotted "
            "segments, for example `garden.checklist`.",
            details={"pack_id": pack_id})
    if mode not in MODES:
        raise InvalidInput(f"Unknown capability mode {mode!r}.",
                           details={"modes": list(MODES)})
    if not TEMPLATE_ROOT.is_dir():
        raise InvalidInput(
            "The capability template is not installed alongside this package.",
            details={"expected": str(TEMPLATE_ROOT)})

    package = Path(output)
    planned = [package / relative for relative in TEMPLATE_FILES]
    # Checked as a set before writing: a pack that is half template and half
    # the author's own work validates as neither.
    existing = [path for path in planned if path.exists()]
    if existing:
        raise Conflict(
            "Refusing to overwrite files that are already there: "
            + ", ".join(sorted(str(p) for p in existing)),
            details={"existing": sorted(str(p) for p in existing)})

    operation = f"{pack_id}.prepare"
    written: list[Path] = []
    for relative in TEMPLATE_FILES:
        source = TEMPLATE_ROOT / relative
        text = source.read_text(encoding="utf-8")
        text = text.replace(TEMPLATE_ID, pack_id)
        if relative == "capability.yaml":
            text = _apply_manifest_choices(text, role=role, mode=mode,
                                           version=version)
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(target)

    return Scaffold(package=package, files=tuple(written), pack_id=pack_id,
                    version=version, operation=operation)


def _apply_manifest_choices(text: str, *, role: str, mode: str,
                            version: str) -> str:
    """Apply the three choices the caller actually made.

    Line-anchored so `mode:` under an operation is rewritten and a `mode`
    appearing inside a description is not. `instructions:` is dropped for
    modes that have no instructions file, because manifest validation rejects
    an adapter operation carrying an agent binding.
    """
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("owner_role:"):
            out.append(f"owner_role: {role}")
            continue
        if stripped.startswith("version:") and not line.startswith(" "):
            out.append(f'version: "{version}"')
            continue
        if stripped.startswith("mode:"):
            out.append(line[:len(line) - len(line.lstrip())] + f"mode: {mode}")
            continue
        if stripped.startswith("instructions:") and mode != "agent":
            continue
        out.append(line)
    return "\n".join(out) + "\n"


def scaffold_summary(result: Scaffold) -> str:
    """One line per created file, in the order they were written."""
    listing = "\n".join(f"  {path}" for path in result.files)
    return (f"{result.pack_id} {result.version} scaffolded at {result.package}\n"
            f"{listing}\n"
            f"Operation: {result.operation} (disabled until you enable it)")


def as_json(result: Scaffold) -> str:
    return json.dumps({"pack_id": result.pack_id, "version": result.version,
                       "package": str(result.package),
                       "operation": result.operation,
                       "files": [str(p) for p in result.files]},
                      indent=2, sort_keys=True)
