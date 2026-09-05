"""Normative logical schema (runtime contract §4).

Storage conventions that apply to every table here:

* Instants persist as **integer UTC microseconds**, never ISO strings or floats.
* Local dates persist as ``YYYY-MM-DD`` **strings**; a day-level obligation is
  never coerced into a UTC-midnight instant.
* Every mutable domain object carries ``id``, ``version`` (from 1),
  ``created_at`` and ``updated_at``. Updates require ``expected_version``;
  a mismatch is a conflict, never last-write-wins.
* Every content-bearing row carries a ``privacy`` JSON column holding a
  serialised :class:`~loop.core.privacy.PrivacyLabel`.

This module defines the Stage A subset of the 30 logical tables — the ones the
durable-commitment path needs. Later stages add the vault, capability, learning
and travel tables against the same conventions and the same migration runner.

``create_all`` is *not* migration (runtime §4). It is used only to materialise a
brand-new database at the current head revision; every change to an existing
database goes through :mod:`loop.db.migrations`.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative base for the vNext schema."""


class IdentityMixin:
    """The common identity/version/time columns required of mutable objects."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class PrivacyMixin:
    """A serialised PrivacyLabel for content-bearing rows."""

    privacy: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


# --------------------------------------------------------------------------- #
# Intake
# --------------------------------------------------------------------------- #
class Event(Base, IdentityMixin, PrivacyMixin):
    """An accepted, deduplicated inbound event.

    ``UNIQUE(origin, idempotency_key)`` is what makes replay safe: a Telegram
    update delivered three times collapses to one row, so downstream work is
    created once (T02).
    """

    __tablename__ = "events"

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    origin: Mapped[str] = mapped_column(String(64), nullable=False)
    origin_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    root_id: Mapped[str] = mapped_column(String(36), nullable=False)
    causation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    hop_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")

    __table_args__ = (
        UniqueConstraint("origin", "idempotency_key", name="uq_events_origin_key"),
        Index("ix_events_root", "root_id"),
    )


# --------------------------------------------------------------------------- #
# Plans and work
# --------------------------------------------------------------------------- #
class Plan(Base, IdentityMixin):
    __tablename__ = "plans"

    root_event_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("events.id"), nullable=False)
    intent_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    policy_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    budget_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    usage_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    deadline_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class WorkItem(Base, IdentityMixin):
    """One role's assignment within a plan.

    ``fencing_token`` is compared before any commit, so a worker whose lease
    expired mid-call cannot write results afterwards (D03).
    """

    __tablename__ = "work_items"

    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("plans.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False, default="")
    inputs_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_schema: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dependencies_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operation_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    operation_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    package_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Artifact(Base, IdentityMixin, PrivacyMixin):
    """An immutable result. Content never changes once written."""

    __tablename__ = "artifacts"

    plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("plans.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")


# --------------------------------------------------------------------------- #
# Commitments
# --------------------------------------------------------------------------- #
class Task(Base, IdentityMixin, PrivacyMixin):
    """A durable commitment.

    ``due_date`` (a day-level obligation) and ``due_at`` (an exact instant) are
    mutually exclusive — enforced in the service layer and asserted by tests,
    because SQLite cannot express the XOR as a portable constraint alongside the
    nullability rules.
    """

    __tablename__ = "tasks"

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    original_event_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("events.id"), nullable=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False, default="owner")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="inbox")
    priority: Mapped[str] = mapped_column(String(8), nullable=False, default="normal")
    project_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    due_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    due_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False,
                                          default="Europe/Berlin")
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    waiting_on: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    context_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    completed_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cancelled_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (Index("ix_tasks_status_due", "status", "due_at"),)


# --------------------------------------------------------------------------- #
# Time
# --------------------------------------------------------------------------- #
class Trigger(Base, IdentityMixin, PrivacyMixin):
    __tablename__ = "triggers"

    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    definition_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_fire_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_fire_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (Index("ix_triggers_enabled_next", "enabled", "next_fire_at"),)


class TriggerFiring(Base):
    """One occurrence of a trigger.

    ``UNIQUE(trigger_id, trigger_revision, occurrence_key)`` is the exactly-once
    guarantee for internal occurrences: a DST fold, a catch-up replay and a
    restart all resolve to the same key, so the reminder fires once (D08, D01).
    """

    __tablename__ = "trigger_firings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    trigger_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("triggers.id"), nullable=False)
    trigger_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_key: Mapped[str] = mapped_column(String(128), nullable=False)
    nominal_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effective_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    plan_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("trigger_id", "trigger_revision", "occurrence_key",
                         name="uq_firing_occurrence"),
    )


class Job(Base, IdentityMixin):
    """A unit of durable background work with a compare-and-set lease."""

    __tablename__ = "jobs"

    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    run_after: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deadline_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fencing_token: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_jobs_dedupe"),
        Index("ix_jobs_state_run_after", "state", "run_after"),
    )


# --------------------------------------------------------------------------- #
# Delivery
# --------------------------------------------------------------------------- #
class Notification(Base, IdentityMixin):
    __tablename__ = "notifications"

    category: Mapped[str] = mapped_column(String(24), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    occurrence_key: Mapped[str] = mapped_column(String(128), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    sensitivity_mode: Mapped[str] = mapped_column(String(16), nullable=False,
                                                  default="full")
    not_before: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    acknowledgement_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    __table_args__ = (
        UniqueConstraint("category", "subject_ref", "occurrence_key",
                         "destination_id", name="uq_notification_occurrence"),
    )


class Outbox(Base, IdentityMixin):
    """Transport queue. ``unknown`` is a first-class state.

    A send that timed out after the provider may have accepted it is recorded
    as ``unknown`` and never blindly resent (D06).
    """

    __tablename__ = "outbox"

    notification_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    provider_receipt: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe"),
        Index("ix_outbox_state_retry", "state", "retry_at"),
    )


# --------------------------------------------------------------------------- #
# Effects and authority
# --------------------------------------------------------------------------- #
class Operation(Base, IdentityMixin):
    """A proposed or performed mutation with deterministic identity."""

    __tablename__ = "operations"

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_version_or_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True)
    policy_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    authority_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operations_idempotency"),
    )


class Approval(Base, IdentityMixin):
    """Single-use authority bound to an operation and its exact input hash."""

    __tablename__ = "approvals"

    operation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("operations.id"), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    expires_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resolved_at: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Feedback(Base, PrivacyMixin):
    __tablename__ = "feedback"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subject_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    occurred_at: Mapped[int] = mapped_column(BigInteger, nullable=False)

    __table_args__ = (
        UniqueConstraint("event_id", "subject_ref", "kind", name="uq_feedback_once"),
    )


class Audit(Base):
    """Append-only decision metadata. Never prompts, bodies, tokens or secrets."""

    __tablename__ = "audit"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    hashes_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    backend: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    token_usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False)


class SchemaVersion(Base):
    """Records which migrations have been applied."""

    __tablename__ = "schema_version"

    revision: Mapped[str] = mapped_column(String(64), primary_key=True)
    applied_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
