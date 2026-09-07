"""Capability objects: versioned domain state without core schema edits (EX13).

A new pack needs somewhere to keep its own records. The two obvious answers are
both wrong: a new core table per domain means every pack edits the core schema,
and an untyped blob means nothing validates until something breaks in
production.

So a capability object is a row in one shared store, whose payload is validated
against a schema the pack registered, and whose writes carry `expected_version`.
Adding a domain adds a schema, not a migration.

**Privacy propagates without a bespoke path.** The label travels with the object,
and a read merges it into the caller's context like any other source — a pack
cannot widen a label by storing data and reading it back.

Schema changes are versioned, and a **breaking** change needs an explicit
migration and authority (EX11). Rollback restores local state; it does not undo
effects that already left the machine, and saying otherwise would be the most
dangerous kind of reassurance.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from loop.core.errors import Conflict, InvalidInput, ValidationFailed
from loop.core.privacy import PrivacyLabel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)


def _now_micros() -> int:
    """A microsecond timestamp for rows that do not carry an injected clock.

    This store has no `clock` parameter (its callers do not currently pass
    one), so the write time is the wall clock rather than a test-controlled
    instant. It is bookkeeping metadata only — no business rule in this module
    reads it back — so this does not affect any decision under test.
    """
    import time

    return int(time.time() * 1_000_000)


class ChangeKind(str, Enum):
    COMPATIBLE = "compatible"
    BREAKING = "breaking"

    @property
    def needs_migration(self) -> bool:
        return self is ChangeKind.BREAKING


@dataclass
class ObjectSchema:
    """A pack's registered payload schema, at one version."""

    pack_id: str
    object_type: str
    schema_version: int
    required: frozenset[str] = frozenset()
    properties: dict[str, str] = field(default_factory=dict)
    effects: frozenset[str] = frozenset()

    @property
    def key(self) -> str:
        return f"{self.pack_id}:{self.object_type}"

    def validate(self, payload: dict[str, Any]) -> list[str]:
        problems = [f"missing required field {name!r}"
                    for name in sorted(self.required) if name not in payload]
        for name, value in sorted(payload.items()):
            expected = self.properties.get(name)
            if expected is None:
                problems.append(f"unknown field {name!r}")
            elif not _matches(value, expected):
                problems.append(
                    f"field {name!r} should be {expected}, got "
                    f"{type(value).__name__}")
        return problems


_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict}


def _matches(value: Any, expected: str) -> bool:
    python_type = _TYPES.get(expected)
    if python_type is None:
        return True
    if expected in ("integer", "number") and isinstance(value, bool):
        return False          # bool is an int subclass; a flag is not a count
    return isinstance(value, python_type)


def diff_schemas(old: ObjectSchema, new: ObjectSchema) -> tuple[ChangeKind, list[str]]:
    """Classify a schema change (EX11).

    Breaking means existing stored objects would no longer validate, or an
    effect was added. A new *optional* field is compatible; a new required one
    is not, because every row already written lacks it.
    """
    reasons: list[str] = []

    for name in sorted(new.required - old.required):
        reasons.append(f"new required field {name!r}")
    for name in sorted(set(old.properties) - set(new.properties)):
        reasons.append(f"removed field {name!r}")
    for name in sorted(set(old.properties) & set(new.properties)):
        if old.properties[name] != new.properties[name]:
            reasons.append(f"field {name!r} changed type "
                           f"{old.properties[name]} → {new.properties[name]}")
    for effect in sorted(new.effects - old.effects):
        reasons.append(f"new effect {effect!r}")

    return (ChangeKind.BREAKING if reasons else ChangeKind.COMPATIBLE), reasons


@dataclass
class CapabilityObject:
    id: str
    pack_id: str
    object_type: str
    schema_version: int
    payload: dict[str, Any]
    version: int = 1
    privacy: PrivacyLabel = field(default_factory=PrivacyLabel)


class CapabilityObjectStore:
    """One shared store for every pack's domain objects.

    Optionally persistent via `sessions`: schemas and objects both write
    through, and construction hydrates from `capability_schemas` and
    `capability_objects`. Without `sessions` this is exactly the in-memory
    object every existing test already constructs.
    """

    def __init__(self, *, sessions: sessionmaker[Session] | None = None
                ) -> None:
        self._schemas: dict[tuple[str, int], ObjectSchema] = {}
        self._latest: dict[str, int] = {}
        self._objects: dict[str, CapabilityObject] = {}
        self._sessions = sessions
        if self._sessions is not None:
            self._ensure_tables()
            self._load()

    # ------------------------------------------------------------------ #
    # Persistence (optional)
    # ------------------------------------------------------------------ #
    def _ensure_tables(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS capability_schemas (
                    pack_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    required_json TEXT NOT NULL DEFAULT '[]',
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    effects_json TEXT NOT NULL DEFAULT '[]',
                    is_latest INTEGER NOT NULL DEFAULT 1,
                    migration TEXT NOT NULL DEFAULT '',
                    authority_event_id TEXT NOT NULL DEFAULT '',
                    created_at BIGINT NOT NULL,
                    PRIMARY KEY (pack_id, object_type, schema_version)
                )"""))
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS capability_objects (
                    id TEXT PRIMARY KEY,
                    pack_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    privacy TEXT,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )"""))
            session.commit()

    def _load(self) -> None:
        assert self._sessions is not None
        with self._sessions() as session:
            schema_rows = session.execute(text(
                "SELECT pack_id, object_type, schema_version, required_json, "
                "properties_json, effects_json, is_latest "
                "FROM capability_schemas")).all()
            object_rows = session.execute(text(
                "SELECT id, pack_id, object_type, schema_version, "
                "payload_json, version, privacy "
                "FROM capability_objects")).all()

        for row in schema_rows:
            schema = ObjectSchema(
                pack_id=row[0], object_type=row[1], schema_version=row[2],
                required=frozenset(json.loads(row[3])),
                properties=json.loads(row[4]),
                effects=frozenset(json.loads(row[5])))
            self._schemas[(schema.key, schema.schema_version)] = schema
            if row[6]:
                self._latest[schema.key] = schema.schema_version

        for row in object_rows:
            self._objects[row[0]] = CapabilityObject(
                id=row[0], pack_id=row[1], object_type=row[2],
                schema_version=row[3], payload=json.loads(row[4]),
                version=row[5],
                privacy=PrivacyLabel.from_json(json.loads(row[6]))
                if row[6] else PrivacyLabel())

    def _persist_schema(self, schema: ObjectSchema, *, is_latest: bool,
                        migration: str, authority_event_id: str,
                        created_at: int) -> None:
        if self._sessions is None:
            return
        with self._sessions() as session:
            if is_latest:
                session.execute(text(
                    "UPDATE capability_schemas SET is_latest = 0 "
                    "WHERE pack_id = :pack AND object_type = :obj_type"),
                    {"pack": schema.pack_id, "obj_type": schema.object_type})
            session.execute(text(
                "INSERT INTO capability_schemas (pack_id, object_type, "
                "schema_version, required_json, properties_json, "
                "effects_json, is_latest, migration, authority_event_id, "
                "created_at) VALUES (:pack, :obj_type, :version, :required, "
                ":properties, :effects, :is_latest, :migration, :authority, "
                ":now)"),
                {"pack": schema.pack_id, "obj_type": schema.object_type,
                 "version": schema.schema_version,
                 "required": json.dumps(sorted(schema.required)),
                 "properties": json.dumps(schema.properties, sort_keys=True),
                 "effects": json.dumps(sorted(schema.effects)),
                 "is_latest": int(is_latest), "migration": migration,
                 "authority": authority_event_id, "now": created_at})
            session.commit()

    def _set_latest_version(self, key: str, version: int) -> None:
        """The single place `_latest` changes, so persistence never drifts
        from the in-memory pointer (used by both registration and rollback)."""
        self._latest[key] = version
        if self._sessions is None:
            return
        pack_id, _, object_type = key.partition(":")
        with self._sessions() as session:
            session.execute(text(
                "UPDATE capability_schemas SET is_latest = "
                "CASE WHEN schema_version = :version THEN 1 ELSE 0 END "
                "WHERE pack_id = :pack AND object_type = :obj_type"),
                {"version": version, "pack": pack_id, "obj_type": object_type})
            session.commit()

    def _persist_object(self, record: CapabilityObject, *,
                        created_at: int, updated_at: int) -> None:
        if self._sessions is None:
            return
        with self._sessions() as session:
            session.execute(text(
                "INSERT INTO capability_objects (id, pack_id, object_type, "
                "schema_version, payload_json, version, privacy, created_at, "
                "updated_at) VALUES (:id, :pack, :obj_type, :schema_version, "
                ":payload, :version, :privacy, :created, :updated) "
                "ON CONFLICT(id) DO UPDATE SET "
                "payload_json = excluded.payload_json, "
                "version = excluded.version, updated_at = excluded.updated_at"),
                {"id": record.id, "pack": record.pack_id,
                 "obj_type": record.object_type,
                 "schema_version": record.schema_version,
                 "payload": json.dumps(record.payload, sort_keys=True),
                 "version": record.version,
                 "privacy": json.dumps(record.privacy.to_json()),
                 "created": created_at, "updated": updated_at})
            session.commit()

    # ------------------------------------------------------------------ #
    # Schemas
    # ------------------------------------------------------------------ #
    def register_schema(self, schema: ObjectSchema, *,
                        migration: str = "", authority_event_id: str = ""
                        ) -> ObjectSchema:
        """Register a schema version, refusing an unauthorised breaking change."""
        current_version = self._latest.get(schema.key)
        if current_version is not None:
            current = self._schemas[(schema.key, current_version)]
            if schema.schema_version <= current.schema_version:
                raise InvalidInput(
                    "A registered schema version is immutable; publish a new "
                    "version instead.",
                    details={"key": schema.key,
                             "existing": current.schema_version})
            kind, reasons = diff_schemas(current, schema)
            if kind.needs_migration and not (migration and authority_event_id):
                raise ValidationFailed(
                    "This is a breaking schema change and needs an explicit "
                    "migration and authority.",
                    details={"key": schema.key, "reasons": reasons})

        self._schemas[(schema.key, schema.schema_version)] = schema
        self._persist_schema(schema, is_latest=True, migration=migration,
                             authority_event_id=authority_event_id,
                             created_at=_now_micros())
        self._set_latest_version(schema.key, schema.schema_version)
        return schema

    def schema(self, key: str, version: int | None = None) -> ObjectSchema | None:
        resolved = version if version is not None else self._latest.get(key)
        if resolved is None:
            return None
        return self._schemas.get((key, resolved))

    # ------------------------------------------------------------------ #
    # Objects
    # ------------------------------------------------------------------ #
    def create(self, *, object_id: str, pack_id: str, object_type: str,
               payload: dict[str, Any],
               privacy: PrivacyLabel | None = None) -> CapabilityObject:
        schema = self._require_schema(f"{pack_id}:{object_type}")
        problems = schema.validate(payload)
        if problems:
            raise ValidationFailed("The payload does not match the registered "
                                   "schema.",
                                   details={"problems": problems})

        record = CapabilityObject(
            id=object_id, pack_id=pack_id, object_type=object_type,
            schema_version=schema.schema_version, payload=dict(payload),
            privacy=privacy or PrivacyLabel())
        self._objects[object_id] = record
        now = _now_micros()
        self._persist_object(record, created_at=now, updated_at=now)
        return record

    def get(self, object_id: str) -> CapabilityObject | None:
        return self._objects.get(object_id)

    def update(self, object_id: str, *, expected_version: int,
               payload: dict[str, Any]) -> CapabilityObject:
        """Apply a change, refusing a stale writer (EX13)."""
        record = self._objects.get(object_id)
        if record is None:
            raise InvalidInput(f"no capability object {object_id!r}")
        if record.version != expected_version:
            raise Conflict(
                "This object changed since you read it.",
                details={"object_id": object_id,
                         "expected_version": expected_version,
                         "current_version": record.version})

        schema = self._require_schema(f"{record.pack_id}:{record.object_type}")
        merged = {**record.payload, **payload}
        problems = schema.validate(merged)
        if problems:
            raise ValidationFailed("The updated payload does not match the "
                                   "registered schema.",
                                   details={"problems": problems})

        record.payload = merged
        record.version += 1
        self._persist_object(record, created_at=_now_micros(),
                             updated_at=_now_micros())
        return record

    def read_with_privacy(self, object_id: str, *,
                          context: PrivacyLabel) -> tuple[dict[str, Any],
                                                          PrivacyLabel]:
        """Read a payload and merge its label into the caller's context.

        Storing private data and reading it back must not launder the label —
        the merge is the same one every other source goes through.
        """
        record = self._objects.get(object_id)
        if record is None:
            raise InvalidInput(f"no capability object {object_id!r}")
        return dict(record.payload), PrivacyLabel.merge([context, record.privacy])

    def _require_schema(self, key: str) -> ObjectSchema:
        schema = self.schema(key)
        if schema is None:
            raise InvalidInput(f"no registered schema for {key!r}")
        return schema


@dataclass
class RollbackResult:
    """What a rollback restored, and what it could not (EX11)."""

    restored_version: int
    preserved_objects: int
    irreversible_effects: list[str] = field(default_factory=list)

    @property
    def fully_reversed(self) -> bool:
        return not self.irreversible_effects


def rollback_schema(store: CapabilityObjectStore, key: str, *,
                    to_version: int,
                    external_effects: list[str] | None = None) -> RollbackResult:
    """Roll a pack back to an earlier schema version.

    Local state is restored; anything that already left the machine is not.
    A rollback that reported "reverted" after an email was sent would be
    describing a state that does not exist anywhere.
    """
    target = store.schema(key, to_version)
    if target is None:
        raise InvalidInput(f"no schema {key!r} at version {to_version}")

    store._set_latest_version(key, to_version)           # noqa: SLF001
    preserved = sum(1 for obj in store._objects.values()  # noqa: SLF001
                    if f"{obj.pack_id}:{obj.object_type}" == key)

    return RollbackResult(restored_version=to_version, preserved_objects=preserved,
                          irreversible_effects=list(external_effects or []))
