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

import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

from loop.core.ids import new_id

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

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
    """Links and scopes deciding what a bulk write may touch.

    Optionally persistent via `sessions`. A remote link is authority to
    *update* a specific remote object, and a scope is authority to *create*
    one under a project — both are decisions with real consequences (an
    unwanted push is visible to other people and cannot be taken back), so
    losing either on restart would either reopen access that was scoped shut
    or silently stop updating an object Loop already owns remotely.
    """

    def __init__(self, links: list[RemoteLink] | None = None,
                 scopes: list[ExportScope] | None = None, *,
                 sessions: sessionmaker[Session] | None = None) -> None:
        self._links: dict[tuple[str, str], RemoteLink] = {
            (link.provider, link.local_id): link for link in links or []}
        self._scopes = list(scopes or [])
        self._sessions = sessions
        if self._sessions is not None:
            self._ensure_tables()
            self._load()
            for link in links or []:
                self._persist_link(link)
            for scope in scopes or []:
                self._persist_scope(scope)

    # ------------------------------------------------------------------ #
    # Persistence (optional)
    # ------------------------------------------------------------------ #
    def _ensure_tables(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS remote_links (
                    provider TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    remote_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    local_id TEXT NOT NULL,
                    remote_version TEXT NOT NULL DEFAULT '',
                    sync_base_json TEXT NOT NULL DEFAULT '{}',
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL,
                    PRIMARY KEY (provider, account_id, remote_id)
                )"""))
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS export_scopes (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    project_ref TEXT NOT NULL,
                    object_types_json TEXT NOT NULL DEFAULT '[]',
                    created_at BIGINT NOT NULL
                )"""))
            session.commit()

    def _load(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            link_rows = session.execute(text(
                "SELECT provider, account_id, remote_id, object_type, "
                "local_id, remote_version FROM remote_links")).all()
            scope_rows = session.execute(text(
                "SELECT provider, project_ref, object_types_json "
                "FROM export_scopes")).all()
        for row in link_rows:
            link = RemoteLink(provider=row[0], account_id=row[1],
                              remote_id=row[2], object_type=row[3],
                              local_id=row[4], remote_version=row[5])
            self._links[(link.provider, link.local_id)] = link
        for row in scope_rows:
            self._scopes.append(ExportScope(
                provider=row[0], project_ref=row[1],
                object_types=frozenset(json.loads(row[2]))))

    def _persist_link(self, link: RemoteLink) -> None:
        if self._sessions is None:
            return
        now = int(time.time() * 1_000_000)
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO remote_links (provider, account_id, remote_id, "
                "object_type, local_id, remote_version, sync_base_json, "
                "created_at, updated_at) VALUES (:provider, :account, "
                ":remote_id, :obj_type, :local_id, :remote_version, '{}', "
                ":now, :now) "
                "ON CONFLICT(provider, account_id, remote_id) DO UPDATE SET "
                "object_type = excluded.object_type, "
                "local_id = excluded.local_id, "
                "remote_version = excluded.remote_version, "
                "updated_at = excluded.updated_at"),
                {"provider": link.provider, "account": link.account_id,
                 "remote_id": link.remote_id, "obj_type": link.object_type,
                 "local_id": link.local_id,
                 "remote_version": link.remote_version, "now": now})
            session.commit()

    def _persist_scope(self, scope: ExportScope) -> None:
        if self._sessions is None:
            return
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO export_scopes (id, provider, project_ref, "
                "object_types_json, created_at) VALUES (:id, :provider, "
                ":project, :types, :now)"),
                {"id": new_id(), "provider": scope.provider,
                 "project": scope.project_ref,
                 "types": json.dumps(sorted(scope.object_types)),
                 "now": int(time.time() * 1_000_000)})
            session.commit()

    def add_link(self, link: RemoteLink) -> RemoteLink:
        self._links[(link.provider, link.local_id)] = link
        self._persist_link(link)
        return link

    def add_scope(self, scope: ExportScope) -> ExportScope:
        self._scopes.append(scope)
        self._persist_scope(scope)
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
