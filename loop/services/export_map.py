"""Remote export mappings (runtime §data, A18).

A "push everything to Wrike" instruction is a statement about the *project*, not
about every row in the database. Personal tasks live in the same tables as work
tasks, and the mapping is what tells them apart.

The rule is that export is opt-in per object: an object with no mapping and no
covering scope is not pushed. Inferring one — by project name, by similarity, by
"it was probably meant" — is how a private note about a doctor's appointment
appears on a shared work board, and no later deletion undoes that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteLink:
    """A confirmed correspondence between a local object and a remote one."""

    provider: str
    account_id: str
    remote_id: str
    object_type: str
    local_id: str
    remote_version: str = ""


@dataclass(frozen=True)
class ExportScope:
    """An authorised region of local objects that may be pushed."""

    provider: str
    project_ref: str
    #: Object types this scope covers; empty means every type.
    object_types: frozenset[str] = frozenset()

    def covers(self, *, provider: str, project_ref: str,
               object_type: str) -> bool:
        if provider != self.provider or project_ref != self.project_ref:
            return False
        return not self.object_types or object_type in self.object_types


@dataclass
class ExportDecision:
    local_id: str
    push: bool
    reason: str
    remote_id: str = ""

    @property
    def skipped(self) -> bool:
        return not self.push


class ExportMap:
    """Links and scopes deciding what a bulk write may touch."""

    def __init__(self, links: list[RemoteLink] | None = None,
                 scopes: list[ExportScope] | None = None) -> None:
        self._links: dict[tuple[str, str], RemoteLink] = {
            (link.provider, link.local_id): link for link in links or []}
        self._scopes = list(scopes or [])

    def add_link(self, link: RemoteLink) -> RemoteLink:
        self._links[(link.provider, link.local_id)] = link
        return link

    def add_scope(self, scope: ExportScope) -> ExportScope:
        self._scopes.append(scope)
        return scope

    def link_for(self, *, provider: str, local_id: str) -> RemoteLink | None:
        return self._links.get((provider, local_id))

    def in_scope(self, *, provider: str, project_ref: str,
                 object_type: str) -> bool:
        return any(scope.covers(provider=provider, project_ref=project_ref,
                                object_type=object_type)
                   for scope in self._scopes)

    def decide(self, *, provider: str, local_id: str, project_ref: str,
               object_type: str) -> ExportDecision:
        """Decide whether one object may be pushed (A18).

        An existing link is authority to *update* that remote object. A covering
        scope is authority to *create* one. Neither present means the object
        stays local — the safe direction, because an unwanted push is visible to
        other people and cannot be taken back.
        """
        link = self.link_for(provider=provider, local_id=local_id)
        if link is not None:
            return ExportDecision(local_id, True, "existing export mapping",
                                  remote_id=link.remote_id)

        if self.in_scope(provider=provider, project_ref=project_ref,
                         object_type=object_type):
            return ExportDecision(local_id, True,
                                  f"covered by an approved {provider} scope")

        return ExportDecision(
            local_id, False,
            f"no {provider} export mapping and no covering scope; this object "
            f"stays local")

    def plan_bulk_push(self, objects: list[dict[str, str]], *,
                       provider: str) -> list[ExportDecision]:
        """Decide a whole bulk write, per object, never per instruction."""
        return [self.decide(provider=provider, local_id=obj["local_id"],
                            project_ref=obj.get("project_ref", ""),
                            object_type=obj.get("object_type", "task"))
                for obj in objects]
