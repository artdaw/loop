"""The seven roles and what each may do (runtime §1, agent-stack §2).

The registry IDs were already validated by the planner, but validation is not
identity: a role that only exists as a string in an allow-list has no
instructions and no tool scope, so every specialist is really the same
unconstrained agent wearing a different label.

This module gives a role the two things the spec says it has — **instructions
and tool scope**. One model may serve every role; what differs is what it is
told and what it is allowed to reach.

**A vault's own agents are a different system, and only partly overlap.**
A vault may carry agent documents under `_ctx/agents/`: Claude Code subagents
with their own tool lists and triggers. Where one covers the same *domain
conventions* as a Loop role of the same name — capture, compilation, retrieval —
Loop consults it as supplementary guidance.

The reference vault also carries `edward`, a data-visualisation critic in the
Tufte tradition. It is **not** a synthesis or review agent, so it is mapped to
no role here.

Runtime §1 says "Edward's synthesis/review work maps to Coordinator/Reviewer".
That does not match the vault as it actually stands: following it literally would
load chart-critique instructions into the coordinator. But Edward's expertise is
real and Loop does have work it fits — Loop renders dashboards, weekly reviews and
metrics for the owner to read, and "does this graph lie" is exactly the right
question to ask of those. So Edward is adopted **as an advisory consultant for one
kind of work**, not as a role's general brief: `ADVISORY_DOCUMENTS` binds him to
presentation critique, which the Reviewer may invoke when reviewing a rendered
artifact. The discrepancy with runtime §1 is recorded as a policy conflict rather
than silently resolved either way.

So:

* **A built-in brief is authoritative for every Loop role.** It defines what the
  role is accountable for inside Loop's runtime.
* **A vault document, where one genuinely corresponds, is consulted in addition.**
  Loop reads it and never writes it (runtime §1: "Do not replace the vault role
  documents"). It supplies the owner's conventions, not Loop's accountability.

Scope is a deny-by-default allow-list per role. A scope that a role does not hold
is refused before the tool is built, not after the model asks for it: an agent
that can see a tool will eventually call it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from loop.core.errors import ValidationFailed

logger = logging.getLogger(__name__)

#: Registry role IDs (runtime §1). Kept here as the single definition; the
#: planner and capability registry both read it.
COORDINATOR = "coordinator"
COMMITMENTS = "commitments"
SCRIBE = "scribe"
COMPILER = "compiler"
SEEKER = "seeker"
DAILY_LIFE = "daily_life"
REVIEWER = "reviewer"

ROLE_IDS = (COORDINATOR, COMMITMENTS, SCRIBE, COMPILER, SEEKER, DAILY_LIFE,
            REVIEWER)


@dataclass(frozen=True)
class RoleDefinition:
    """What one role is told, and what it may reach."""

    id: str
    summary: str
    #: Capability scopes this role may use. Deny by default.
    scopes: frozenset[str]
    #: A vault agent document covering the same domain conventions, if one
    #: genuinely corresponds. Consulted, never adopted wholesale.
    vault_document: str = ""
    #: Loop's own brief for the role. Always authoritative.
    builtin_instructions: str = ""
    #: Roles that may not propose mutations at all.
    read_only: bool = False

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


#: The seven roles. Scopes mirror runtime §1's "Owns" column: a role's tools are
#: the ones it needs for what it owns, and nothing else.
ROLE_DEFINITIONS: dict[str, RoleDefinition] = {
    COORDINATOR: RoleDefinition(
        id=COORDINATOR,
        summary="Owns user intent, decomposition, dependencies and the final "
                "response.",
        scopes=frozenset({"plan.compose", "vault.read", "task.read"}),
        # No vault document: `edward.md` is a visualisation critic, not a
        # coordinator. Mapping it here would be worse than having nothing.
        builtin_instructions=(
            "Decompose the owner's request into typed assignments for other "
            "roles. Do not perform domain work yourself, and do not answer "
            "from memory: compose the reply from persisted results only."),
        read_only=True),
    COMMITMENTS: RoleDefinition(
        id=COMMITMENTS,
        summary="Owns tasks, reminders, follow-ups and waiting states.",
        scopes=frozenset({"task.read", "task.write", "trigger.propose",
                          "vault.read", "email.send"}),
        builtin_instructions=(
            "Propose task mutations and triggers. A task with no date is normal. "
            "Never claim a reminder is scheduled before a trigger is persisted.")),
    SCRIBE: RoleDefinition(
        id=SCRIBE,
        summary="Owns exact captures and source registration.",
        scopes=frozenset({"vault.read", "vault.capture"}),
        vault_document="_ctx/agents/scribe.md",
        builtin_instructions=(
            "Capture the owner's words exactly. Do not summarise, correct or "
            "tidy a capture: the raw file is evidence, not a draft.")),
    COMPILER: RoleDefinition(
        id=COMPILER,
        summary="Owns source-grounded knowledge maintenance.",
        scopes=frozenset({"vault.read", "vault.write", "vault.search"}),
        vault_document="_ctx/agents/compiler.md",
        builtin_instructions=(
            "Propose wiki patches grounded in registered sources. Preserve "
            "contradictions rather than resolving them, and never invent a "
            "concept to satisfy a graph shape.")),
    SEEKER: RoleDefinition(
        id=SEEKER,
        summary="Owns retrieval, questions and authorized external research.",
        scopes=frozenset({"vault.read", "vault.search", "research.search",
                          "research.fetch"}),
        vault_document="_ctx/agents/seeker.md",
        builtin_instructions=(
            "Check compiled knowledge before researching. Cite what was actually "
            "read; a search snippet is not a source."),
        read_only=True),
    DAILY_LIFE: RoleDefinition(
        id=DAILY_LIFE,
        summary="Owns weather, calendar context, travel and practical "
                "preparation.",
        scopes=frozenset({"vault.read", "calendar.read", "weather.read",
                          "travel.plan", "notification.propose"}),
        builtin_instructions=(
            "Produce practical preparation from evidence with its provenance. "
            "Missing data is unknown, never a clear forecast.")),
    REVIEWER: RoleDefinition(
        id=REVIEWER,
        summary="Owns outcome checks, weekly review and preference hypotheses.",
        scopes=frozenset({"vault.read", "task.read", "outcome.read",
                          "presentation.critique"}),
        builtin_instructions=(
            "Check results against their evidence and report findings. A review "
            "grants no permission and authorises nothing; deterministic "
            "validators run regardless of what you conclude."),
        read_only=True),
}

@dataclass(frozen=True)
class AdvisoryDocument:
    """A vault agent consulted for one kind of work, not for a whole role.

    The distinction matters: a role's brief says what it is accountable for, and
    inherits its scope. An advisory document contributes expertise to a single
    operation and grants nothing. Edward can tell the Reviewer that a chart's
    axis is misleading; he cannot thereby gain the ability to write files.
    """

    id: str
    path: str
    applies_to_operation: str
    consulted_by: frozenset[str]
    why: str


#: Vault agents adopted for specific work rather than as role briefs.
ADVISORY_DOCUMENTS: tuple[AdvisoryDocument, ...] = (
    AdvisoryDocument(
        id="edward",
        path="_ctx/agents/edward.md",
        applies_to_operation="presentation.critique",
        consulted_by=frozenset({REVIEWER}),
        why="Loop renders dashboards, weekly reviews and metrics for the owner "
            "to read. Edward's subject is whether a graphic tells the truth "
            "clearly, which is the right check for those artifacts and the "
            "wrong instruction set for coordinating work."),
)


#: Frozen view for validators that only need the names.
ROLES = frozenset(ROLE_DEFINITIONS)


def get_role(role_id: str) -> RoleDefinition:
    definition = ROLE_DEFINITIONS.get(role_id)
    if definition is None:
        raise ValidationFailed(
            f"Unknown role {role_id!r}.",
            details={"known_roles": sorted(ROLE_DEFINITIONS)})
    return definition


@dataclass
class RoleInstructions:
    """A role's brief, plus any vault conventions consulted alongside it."""

    role_id: str
    text: str
    source: str = "builtin"
    #: The owner's own conventions for this domain, when a vault document
    #: corresponds. Supplementary: it never replaces `text`.
    vault_guidance: str = ""
    vault_source: str = ""

    @property
    def consulted_vault(self) -> bool:
        return bool(self.vault_guidance)

    @property
    def attribution(self) -> str:
        if self.consulted_vault:
            return (f"Loop's role brief, with the owner's conventions from "
                    f"{self.vault_source}")
        return "Loop's role brief"

    def prompt(self) -> str:
        """What the model is actually told, with the two parts kept distinct."""
        if not self.consulted_vault:
            return self.text
        return (f"{self.text}\n\n"
                f"The owner's conventions for this domain, from "
                f"{self.vault_source} (guidance, not authority):\n"
                f"{self.vault_guidance}")


class RoleRegistry:
    """Resolves role instructions and enforces role scope."""

    def __init__(self, *, vault_root: Path | None = None,
                 max_document_bytes: int = 64 * 1024) -> None:
        self.vault_root = vault_root
        self.max_document_bytes = max_document_bytes
        self._cache: dict[str, RoleInstructions] = {}

    def instructions(self, role_id: str) -> RoleInstructions:
        """Loop's brief for the role, plus any corresponding vault conventions."""
        cached = self._cache.get(role_id)
        if cached is not None:
            return cached

        definition = get_role(role_id)
        resolved = RoleInstructions(role_id=role_id,
                                    text=definition.builtin_instructions)

        guidance, source = self._vault_guidance(definition)
        if guidance:
            resolved.vault_guidance = guidance
            resolved.vault_source = source

        self._cache[role_id] = resolved
        return resolved

    def _vault_guidance(self, definition: RoleDefinition) -> tuple[str, str]:
        """Read the corresponding vault document, if the owner has one."""
        if self.vault_root is None or not definition.vault_document:
            return "", ""
        path = self.vault_root / definition.vault_document
        try:
            if not path.is_file():
                return "", ""
            body = path.read_text(encoding="utf-8")[:self.max_document_bytes]
        except OSError as exc:
            # An unreadable document is a configuration problem, not a reason to
            # run with no role brief — the built-in one still applies.
            logger.warning("Could not read vault agent document %s: %s", path, exc)
            return "", ""
        return (body, definition.vault_document) if body.strip() else ("", "")

    # ------------------------------------------------------------------ #
    # Scope
    # ------------------------------------------------------------------ #
    def allowed_scopes(self, role_id: str) -> frozenset[str]:
        return get_role(role_id).scopes

    def require_scope(self, role_id: str, scope: str) -> None:
        """Refuse a scope the role does not hold (runtime §1)."""
        definition = get_role(role_id)
        if not definition.allows(scope):
            raise ValidationFailed(
                f"Role {role_id!r} may not use {scope!r}.",
                details={"role": role_id, "required_scope": scope,
                         "allowed": sorted(definition.scopes)})

    def filter_operations(self, role_id: str,
                          operations: dict[str, str]) -> dict[str, str]:
        """Narrow a capability catalogue to what this role may reach.

        Filtering happens before the shortlist is disclosed to a model, because
        an operation a role cannot use is not a choice it should be offered —
        agent-stack §2 requires filtering before model disclosure.
        """
        definition = get_role(role_id)
        return {name: description for name, description in operations.items()
                if _scope_of(name) in definition.scopes}

    def may_mutate(self, role_id: str) -> bool:
        return not get_role(role_id).read_only


    # ------------------------------------------------------------------ #
    # Advisory consultation
    # ------------------------------------------------------------------ #
    def advisories_for(self, role_id: str,
                       operation: str) -> list[AdvisoryDocument]:
        """Vault agents this role may consult for this specific operation."""
        return [doc for doc in ADVISORY_DOCUMENTS
                if role_id in doc.consulted_by
                and doc.applies_to_operation == operation]

    def advisory_text(self, document: AdvisoryDocument) -> str:
        """Read an advisory document, or return nothing if it is absent.

        A missing advisory degrades the critique; it never blocks the run. The
        owner may not have that agent, and Loop does not create it.
        """
        if self.vault_root is None:
            return ""
        path = self.vault_root / document.path
        try:
            if not path.is_file():
                return ""
            return path.read_text(encoding="utf-8")[:self.max_document_bytes]
        except OSError as exc:
            logger.warning("Could not read advisory document %s: %s", path, exc)
            return ""

    def consultation_prompt(self, role_id: str, operation: str) -> str:
        """The advisory text to add for one operation, with its attribution."""
        parts: list[str] = []
        for document in self.advisories_for(role_id, operation):
            body = self.advisory_text(document)
            if not body:
                continue
            parts.append(
                f"Consulting {document.id} ({document.path}) for this "
                f"critique. Advisory expertise only — it grants no permission "
                f"and decides nothing:\n{body}")
        return "\n\n".join(parts)


def _scope_of(operation_name: str) -> str:
    """The scope an operation belongs to: its namespace plus its verb class.

    `weather.forecast` needs `weather.read`; `task.create` needs `task.write`.
    Deriving it keeps a pack from choosing its own scope name and thereby
    choosing its own permissions.
    """
    namespace, _, verb = operation_name.partition(".")
    if not namespace:
        return operation_name
    if verb in _OUTWARD_VERBS:
        # Reaching another person or system is its own scope. Falling through
        # to `.read` would have made sending an email the same permission as
        # reading one, which is the difference that matters most.
        return f"{namespace}.send"
    if verb in _WRITE_VERBS:
        return f"{namespace}.write"
    if verb in _PROPOSE_VERBS:
        return f"{namespace}.propose"
    if verb in _SEARCH_VERBS:
        return f"{namespace}.search"
    if verb in _PLAN_VERBS:
        return f"{namespace}.plan"
    if verb in _FETCH_VERBS:
        return f"{namespace}.fetch"
    if verb == "capture":
        return f"{namespace}.capture"
    return f"{namespace}.read"


#: Verbs whose effect leaves the machine. Approval-gated, and never a read.
_OUTWARD_VERBS = frozenset({"send", "publish", "post", "book", "pay", "order",
                            "reply", "invite"})
_WRITE_VERBS = frozenset({"create", "update", "write", "delete", "complete",
                          "cancel", "save", "commit", "patch"})
_PROPOSE_VERBS = frozenset({"propose", "schedule", "suggest"})
_SEARCH_VERBS = frozenset({"search", "query", "find"})
_PLAN_VERBS = frozenset({"plan", "revise", "select", "monitor"})
_FETCH_VERBS = frozenset({"fetch", "get_page", "download"})
