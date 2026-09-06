"""Capability registry: discovery, validation and version pinning (capabilities §2–§4).

The extension promise is that adding a pack requires **no change** to the
coordinator, the core schema, channel handlers or the scheduler. That only holds
if the registry does all the work generically — which means validation here has
to be thorough enough that the executor never needs a special case.

Validation runs **without importing adapter code** and without any network. A
pack is data until it is explicitly enabled, so a malformed or hostile manifest
is caught before it can execute anything (EX03). Specifically refused:

* schema ``$ref``s that leave the package, and any path traversal or symlink
  escape — a manifest is untrusted input like any other file (EX03),
* an operation name already owned by another pack or a builtin namespace, so a
  pack cannot quietly take over ``weather.forecast`` (EX03),
* dependency cycles across packs, and dependencies on operations that do not
  exist (EX04).

**Effects are declarations, not grants.** A manifest listing ``remote_write`` and
a destination is stating what it *would need*; the executor still requires real
scoped authority at call time. A pack cannot widen its own permissions by
declaring more of them (EX08).

Versions are immutable. Changed bytes claiming the same version are rejected
rather than hot-swapped, because a job pinned to a version must be able to trust
that the bytes behind it have not moved underneath it (EX10).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from loop.agents.roles import ROLES as _ROLE_IDS_FROZEN
from loop.core.errors import Conflict, InvalidInput, ValidationFailed
from loop.core.ids import content_hash

logger = logging.getLogger(__name__)

#: Pack and operation naming rules (capabilities §2).
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{0,79}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")

#: Namespaces reserved for builtin operations. A pack cannot claim these.
RESERVED_NAMESPACES = ("task.", "reminder.", "vault.", "notification.",
                       "approval.", "routine.", "preference.", "output.")

#: The complete set of declarable effects (capabilities §3).
VALID_EFFECTS = frozenset({
    "local_artifact_write", "local_domain_write", "vault_write", "network_read",
    "owner_notification_proposal", "remote_write", "spend",
})

VALID_MODES = frozenset({"agent", "workflow", "adapter"})
VALID_BUDGET_CLASSES = frozenset({"interactive", "background", "research"})
#: One definition, in `loop.agents.roles`. Restating it here is how the two
#: validators end up disagreeing about what a valid role is.
ROLES = _ROLE_IDS_FROZEN

#: Refuse absurdly large manifests before parsing them.
MAX_MANIFEST_BYTES = 256 * 1024


class Availability(str, Enum):
    """Why a pack can or cannot be used right now (capabilities §4)."""

    DISCOVERED = "discovered"
    INVALID = "invalid"
    DISABLED = "disabled"
    MISSING_DEPENDENCIES = "missing_dependencies"
    PERMISSION_REQUIRED = "permission_required"
    READY = "ready"
    DEGRADED = "degraded"


@dataclass
class OperationDef:
    """One registered operation."""

    name: str
    description: str
    mode: str
    input_schema: str
    output_schema: str
    instructions: str | None = None
    workflow: str | None = None
    handler: str | None = None
    tools: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    timeout_seconds: int = 60
    retry_policy: str = "none"
    idempotency: str = "root_effect_slot"
    result_artifact_kind: str = "generic"
    default_arguments: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_approval(self) -> bool:
        """Whether this operation's declared effects reach outside the machine."""
        return any(effect in ("remote_write", "spend") for effect in self.effects)


@dataclass
class Manifest:
    """A validated capability.yaml."""

    id: str
    version: str
    title: str
    description: str
    owner_role: str
    operations: dict[str, OperationDef] = field(default_factory=dict)
    support_roles: list[str] = field(default_factory=list)
    policy_namespace: str = ""
    enabled_by_default: bool = False
    budget_class: str = "interactive"
    required_dependencies: list[str] = field(default_factory=list)
    optional_dependencies: list[str] = field(default_factory=list)
    package_path: Path | None = None
    package_hash: str = ""

    @property
    def pack_key(self) -> str:
        return f"{self.id}@{self.version}"


@dataclass
class RegistryEntry:
    """A pack version as the registry holds it."""

    manifest: Manifest
    availability: Availability
    enabled: bool = False
    validation_errors: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return self.enabled and self.availability is Availability.READY


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _resolve_within(package: Path, relative: str, problems: list[str],
                    label: str) -> Path | None:
    """Resolve a package-relative path, refusing any escape."""
    if not relative:
        problems.append(f"{label}: missing path")
        return None
    candidate = Path(relative)
    # A cheap syntactic rejection before touching the filesystem. The resolve
    # check below is the authoritative one and catches symlink escapes too;
    # this exists so an obviously hostile path never reaches a stat() call.
    if candidate.is_absolute() or ".." in candidate.parts:
        problems.append(f"{label}: path escapes the package ({relative!r})")
        return None

    target = package / candidate
    try:
        resolved = target.resolve()
        resolved.relative_to(package.resolve())
    except (ValueError, OSError):
        # A symlink pointing outside the package resolves elsewhere.
        problems.append(f"{label}: path escapes the package ({relative!r})")
        return None
    if not resolved.exists():
        problems.append(f"{label}: file not found ({relative!r})")
        return None
    return resolved


def _check_schema_refs(schema_path: Path, package: Path,
                       problems: list[str], label: str) -> None:
    """Refuse remote or escaping ``$ref``s (capabilities §2).

    Validation must never fetch a remote schema: a pack that can make the
    validator perform a network request has an egress channel before it is even
    enabled.
    """
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        problems.append(f"{label}: unreadable schema ({exc.__class__.__name__})")
        return

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref.startswith(("http://", "https://", "//")):
                    problems.append(f"{label}: remote $ref is not allowed ({ref})")
                elif not ref.startswith("#"):
                    _resolve_within(package, ref.split("#")[0], problems,
                                    f"{label} $ref")
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)


def load_manifest(package: Path) -> tuple[Manifest | None, list[str]]:
    """Parse and validate one pack version. Never imports pack code."""
    problems: list[str] = []
    manifest_path = package / "capability.yaml"

    if not manifest_path.is_file():
        return None, [f"no capability.yaml in {package}"]
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        return None, ["capability.yaml exceeds the size limit"]

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return None, [f"invalid YAML: {exc.__class__.__name__}"]
    if not isinstance(raw, dict):
        return None, ["capability.yaml must be a mapping"]

    if raw.get("schema_version") != 1:
        problems.append("schema_version must be 1")

    pack_id = str(raw.get("id") or "")
    version = str(raw.get("version") or "")
    if not NAME_PATTERN.match(pack_id):
        problems.append(f"invalid pack id {pack_id!r}")
    if not VERSION_PATTERN.match(version):
        problems.append(f"invalid version {version!r} (want major.minor.patch)")

    owner_role = str(raw.get("owner_role") or "")
    if owner_role not in ROLES:
        problems.append(f"unknown owner_role {owner_role!r}")

    defaults = raw.get("defaults") or {}
    budget_class = str(defaults.get("budget_class") or "interactive")
    if budget_class not in VALID_BUDGET_CLASSES:
        problems.append(f"unknown budget_class {budget_class!r}")
    if defaults.get("enabled") is True:
        # A pack cannot enable itself; enablement is an explicit user action.
        problems.append("defaults.enabled must be false; enabling is a user action")

    operations: dict[str, OperationDef] = {}
    raw_operations = raw.get("operations") or {}
    if not isinstance(raw_operations, dict) or not raw_operations:
        problems.append("at least one operation is required")
        raw_operations = {}

    for name, spec in raw_operations.items():
        operations_problems = _validate_operation(str(name), spec, package)
        problems.extend(operations_problems)
        if not operations_problems and isinstance(spec, dict):
            operations[str(name)] = _build_operation(str(name), spec)

    dependencies = raw.get("dependencies") or {}
    required = [str(d) for d in (dependencies.get("required") or [])]
    optional = [str(d) for d in (dependencies.get("optional") or [])]

    # Every listed tool must be a declared dependency (capabilities §3).
    for operation in operations.values():
        for tool in operation.tools:
            if tool not in required and tool not in optional and \
                    tool not in operations:
                problems.append(
                    f"{operation.name}: tool {tool!r} is not a declared dependency")

    if problems:
        return None, problems

    manifest = Manifest(
        id=pack_id, version=version, title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""), owner_role=owner_role,
        operations=operations,
        support_roles=[str(r) for r in (raw.get("support_roles") or [])],
        policy_namespace=str(raw.get("policy_namespace") or pack_id),
        enabled_by_default=False, budget_class=budget_class,
        required_dependencies=required, optional_dependencies=optional,
        package_path=package, package_hash=hash_package(package))
    return manifest, []


def _validate_operation(name: str, spec: Any, package: Path) -> list[str]:
    problems: list[str] = []
    if not NAME_PATTERN.match(name):
        problems.append(f"invalid operation name {name!r}")
    if any(name.startswith(prefix) for prefix in RESERVED_NAMESPACES):
        problems.append(f"{name!r} is in a reserved builtin namespace")
    if not isinstance(spec, dict):
        return [*problems, f"{name}: definition must be a mapping"]

    mode = str(spec.get("mode") or "")
    if mode not in VALID_MODES:
        problems.append(f"{name}: unknown mode {mode!r}")

    # Exactly one binding applies to each mode.
    bindings = {"agent": "instructions", "workflow": "workflow",
                "adapter": "handler"}
    expected = bindings.get(mode)
    if expected and not spec.get(expected):
        problems.append(f"{name}: mode {mode!r} requires {expected!r}")
    for other_mode, other_key in bindings.items():
        if other_mode != mode and spec.get(other_key):
            problems.append(
                f"{name}: {other_key!r} is not valid for mode {mode!r}")

    for key in ("input_schema", "output_schema"):
        resolved = _resolve_within(package, str(spec.get(key) or ""), problems,
                                   f"{name}.{key}")
        if resolved is not None:
            _check_schema_refs(resolved, package, problems, f"{name}.{key}")

    if mode == "agent":
        _resolve_within(package, str(spec.get("instructions") or ""), problems,
                        f"{name}.instructions")
    if mode == "workflow":
        _resolve_within(package, str(spec.get("workflow") or ""), problems,
                        f"{name}.workflow")

    for effect in (spec.get("effects") or []):
        if str(effect) not in VALID_EFFECTS:
            problems.append(f"{name}: unknown effect {effect!r}")

    for reserved in ("authority", "owner", "privacy", "approved"):
        if reserved in (spec.get("default_arguments") or {}):
            problems.append(
                f"{name}: default_arguments may not contain {reserved!r}")

    return problems


def _build_operation(name: str, spec: dict[str, Any]) -> OperationDef:
    return OperationDef(
        name=name, description=str(spec.get("description") or ""),
        mode=str(spec.get("mode")), input_schema=str(spec.get("input_schema")),
        output_schema=str(spec.get("output_schema")),
        instructions=spec.get("instructions"), workflow=spec.get("workflow"),
        handler=spec.get("handler"),
        tools=[str(t) for t in (spec.get("tools") or [])],
        effects=[str(e) for e in (spec.get("effects") or [])],
        destinations=[str(d) for d in (spec.get("destinations") or [])],
        intents=[str(i) for i in (spec.get("intents") or [])],
        timeout_seconds=int(spec.get("timeout_seconds") or 60),
        retry_policy=str(spec.get("retry_policy") or "none"),
        idempotency=str(spec.get("idempotency") or "root_effect_slot"),
        result_artifact_kind=str(spec.get("result_artifact_kind") or "generic"),
        default_arguments=dict(spec.get("default_arguments") or {}))


def hash_package(package: Path) -> str:
    """Content hash over every file in a pack version.

    Any byte change produces a different hash, which is what lets the registry
    detect a pack claiming an immutable version it has since edited (EX10).
    """
    digest_input: list[str] = []
    for path in sorted(package.rglob("*")):
        if path.is_file():
            relative = path.relative_to(package).as_posix()
            digest_input.append(f"{relative}:{content_hash(path.read_bytes())}")
    return content_hash("\n".join(digest_input))


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
class CapabilityRegistry:
    """Holds discovered pack versions and resolves operations."""

    def __init__(self, *, roots: list[Path] | None = None,
                 max_depth: int = 2) -> None:
        self._roots = [Path(r) for r in (roots or [])]
        self._max_depth = max_depth
        self._entries: dict[str, RegistryEntry] = {}     # pack_key -> entry
        self._operation_owner: dict[str, str] = {}       # operation -> pack id
        self.revision = 0

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def discover(self) -> list[RegistryEntry]:
        """Scan configured roots. No network, model or credential involved."""
        found: list[RegistryEntry] = []
        for root in self._roots:
            if not root.is_dir():
                continue
            for package in self._candidate_packages(root):
                manifest, problems = load_manifest(package)
                if manifest is None:
                    logger.info("Invalid pack at %s: %s", package, problems)
                    found.append(RegistryEntry(
                        manifest=Manifest(id=package.name, version="0.0.0",
                                          title="", description="",
                                          owner_role="daily_life",
                                          package_path=package),
                        availability=Availability.INVALID,
                        validation_errors=problems))
                    continue
                found.append(RegistryEntry(manifest=manifest,
                                           availability=Availability.DISCOVERED))
        return found

    def _candidate_packages(self, root: Path) -> list[Path]:
        """Roots holding a manifest, or ``<pack-id>/<version>`` children.

        Bounded by depth so pointing the setting at a home directory does not
        crawl it (capabilities §4).
        """
        packages: list[Path] = []
        if (root / "capability.yaml").is_file():
            return [root]
        for pack_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if (pack_dir / "capability.yaml").is_file():
                packages.append(pack_dir)
                continue
            if self._max_depth < 2:
                continue
            for version_dir in sorted(p for p in pack_dir.iterdir() if p.is_dir()):
                if (version_dir / "capability.yaml").is_file():
                    packages.append(version_dir)
        return packages

    # ------------------------------------------------------------------ #
    # Registration
    # ------------------------------------------------------------------ #
    def register(self, entry: RegistryEntry) -> RegistryEntry:
        """Add a validated pack version, refusing name collisions."""
        manifest = entry.manifest
        existing = self._entries.get(manifest.pack_key)
        if existing is not None:
            if existing.manifest.package_hash != manifest.package_hash:
                # Immutable versions: changed bytes must bump the version, or a
                # pinned job cannot trust what it pinned (EX10).
                raise Conflict(
                    f"{manifest.pack_key} already exists with different content; "
                    "bump the version instead of editing it in place.",
                    details={"pack": manifest.pack_key})
            return existing

        for name in manifest.operations:
            owner = self._operation_owner.get(name)
            if owner is not None and owner != manifest.id:
                raise Conflict(
                    f"Operation {name!r} is already owned by pack {owner!r}.",
                    details={"operation": name, "owner": owner})

        self._entries[manifest.pack_key] = entry
        for name in manifest.operations:
            self._operation_owner[name] = manifest.id
        self.revision += 1
        return entry

    def register_all(self, entries: list[RegistryEntry]) -> list[RegistryEntry]:
        return [self.register(e) for e in entries
                if e.availability is not Availability.INVALID]

    # ------------------------------------------------------------------ #
    # Dependencies
    # ------------------------------------------------------------------ #
    def resolve_availability(self, pack_key: str) -> Availability:
        """Recompute availability from dependencies and enablement (EX04)."""
        entry = self._entries.get(pack_key)
        if entry is None:
            raise InvalidInput(f"Unknown pack {pack_key!r}")
        if entry.availability is Availability.INVALID:
            return Availability.INVALID

        missing = self.missing_dependencies(pack_key)
        if missing:
            entry.availability = Availability.MISSING_DEPENDENCIES
        elif not entry.enabled:
            entry.availability = Availability.DISABLED
        else:
            entry.availability = Availability.READY
        return entry.availability

    def missing_dependencies(self, pack_key: str) -> list[str]:
        entry = self._entries[pack_key]
        known = set(self._operation_owner) | set(BUILTIN_OPERATIONS)
        return [d for d in entry.manifest.required_dependencies if d not in known]

    def dependency_cycles(self) -> list[list[str]]:
        """Detect cycles across packs (EX04).

        A cycle means neither pack can ever become ready, so it must surface as
        a blocked state rather than an infinite resolution attempt.
        """
        graph: dict[str, set[str]] = {}
        for entry in self._entries.values():
            pack = entry.manifest.id
            graph.setdefault(pack, set())
            for dependency in entry.manifest.required_dependencies:
                owner = self._operation_owner.get(dependency)
                if owner and owner != pack:
                    graph[pack].add(owner)

        cycles: list[list[str]] = []
        state: dict[str, int] = dict.fromkeys(graph, 0)

        def visit(node: str, path: list[str]) -> None:
            if state.get(node) == 1:
                cycles.append([*path[path.index(node):], node])
                return
            if state.get(node) == 2:
                return
            state[node] = 1
            for neighbour in graph.get(node, set()):
                visit(neighbour, [*path, node])
            state[node] = 2

        for node in graph:
            if state[node] == 0:
                visit(node, [])
        return cycles

    # ------------------------------------------------------------------ #
    # Enablement
    # ------------------------------------------------------------------ #
    def enable(self, pack_id: str, version: str) -> RegistryEntry:
        """Enable exactly one version of a pack (capabilities §4)."""
        key = f"{pack_id}@{version}"
        entry = self._entries.get(key)
        if entry is None:
            raise InvalidInput(f"Unknown pack version {key!r}")
        if entry.availability is Availability.INVALID:
            raise ValidationFailed(
                f"{key} failed validation and cannot be enabled.",
                details={"problems": entry.validation_errors})

        for other_key, other in self._entries.items():
            if other.manifest.id == pack_id and other_key != key:
                other.enabled = False
                other.availability = Availability.DISABLED

        entry.enabled = True
        self.revision += 1
        self.resolve_availability(key)
        return entry

    def disable(self, pack_id: str) -> list[str]:
        """Disable every version of a pack. Returns the keys affected."""
        affected = []
        for key, entry in self._entries.items():
            if entry.manifest.id == pack_id and entry.enabled:
                entry.enabled = False
                entry.availability = Availability.DISABLED
                affected.append(key)
        if affected:
            self.revision += 1
        return affected

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def resolve_operation(self, name: str) -> tuple[Manifest, OperationDef] | None:
        """Find the enabled implementation of an operation, if any."""
        for entry in self._entries.values():
            if not entry.is_usable:
                continue
            operation = entry.manifest.operations.get(name)
            if operation is not None:
                return entry.manifest, operation
        return None

    def enabled_operations(self) -> dict[str, OperationDef]:
        found: dict[str, OperationDef] = {}
        for entry in self._entries.values():
            if entry.is_usable:
                found.update(entry.manifest.operations)
        return found

    def entries(self) -> list[RegistryEntry]:
        return list(self._entries.values())

    def get(self, pack_key: str) -> RegistryEntry | None:
        return self._entries.get(pack_key)

    def snapshot(self) -> dict[str, Any]:
        """An immutable view used to build tool wrappers for one call."""
        return {
            "revision": self.revision,
            "operations": {
                name: {"pack": entry.manifest.id,
                       "version": entry.manifest.version,
                       "package_hash": entry.manifest.package_hash,
                       "mode": operation.mode,
                       "effects": list(operation.effects)}
                for entry in self._entries.values() if entry.is_usable
                for name, operation in entry.manifest.operations.items()
            },
        }


#: Operations the application itself provides; packs may depend on these.
BUILTIN_OPERATIONS = frozenset({
    "task.create", "task.update", "task.complete",
    "reminder.schedule", "reminder.snooze", "reminder.cancel",
    "vault.search", "vault.read", "vault.capture", "vault.compile",
    "notification.propose", "approval.request", "output.draft",
})
