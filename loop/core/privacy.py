"""Privacy labels and their merge algebra (runtime contract §3).

Every content-bearing object, artifact, event, result, prompt, tool query, cache,
embedding, intermediate plan, error and feedback record carries a
:class:`PrivacyLabel`. It is not a property of "the original message" only.

The merge rules are deliberately asymmetric:

======================  ==========  ==================================
Field                   Combine     Effect
======================  ==========  ==================================
``model_scope``         OR          any local-only input wins
``sensitive``           OR          any sensitive input wins
``origins``             union       provenance accumulates
``allowed_destinations`` **intersection**  permissions shrink
======================  ==========  ==================================

Restrictions spread and permissions shrink, so combining inputs can never yield
a label permitting more than some input already permitted. That single property
is what lets the runtime merge freely without auditing each call site.

An empty destination set permits local storage and use only. Unlabelled imported
content defaults to local-only with no destination — the safe end, chosen because
an import whose provenance nobody recorded is exactly the case where guessing
"probably fine" is most likely to be wrong.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


class ModelScope(str, enum.Enum):
    """Which class of model may see the labelled content."""

    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED = "cloud_allowed"


def _frozen(values: Iterable[str] | None) -> frozenset[str]:
    return frozenset(str(v) for v in (values or ()))


@dataclass(frozen=True, slots=True)
class PrivacyLabel:
    """The privacy classification attached to a piece of content."""

    model_scope: ModelScope = ModelScope.LOCAL_ONLY
    origins: frozenset[str] = field(default_factory=frozenset)
    allowed_destinations: frozenset[str] = field(default_factory=frozenset)
    sensitive: bool = False
    policy_revision: str | None = None

    def __post_init__(self) -> None:
        # Accept sets/lists at the boundary but store immutably, so a label
        # handed to several subsystems cannot be mutated by any of them.
        object.__setattr__(self, "origins", _frozen(self.origins))
        object.__setattr__(self, "allowed_destinations",
                           _frozen(self.allowed_destinations))
        if not isinstance(self.model_scope, ModelScope):
            object.__setattr__(self, "model_scope",
                               _parse_scope(self.model_scope))

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    @property
    def is_local_only(self) -> bool:
        return self.model_scope is ModelScope.LOCAL_ONLY

    def permits_destination(self, destination_id: str) -> bool:
        """True when this label allows delivery to ``destination_id``.

        An empty set is *no* destinations, never "all" — the restrictive
        reading, so a label nobody configured cannot authorise a send.
        """
        return destination_id in self.allowed_destinations

    # ------------------------------------------------------------------ #
    # Combination
    # ------------------------------------------------------------------ #
    @classmethod
    def merge(cls, labels: Iterable[PrivacyLabel]) -> PrivacyLabel:
        """Combine labels so restrictions spread and permissions shrink."""
        items = list(labels)
        if not items:
            # Merging nothing yields the safe default rather than a permissive
            # identity element; there is no "neutral" label that is safe to
            # combine with a local-only one.
            return cls.for_unlabelled_import()
        if len(items) == 1:
            return items[0]

        local_only = any(item.is_local_only for item in items)
        destinations: frozenset[str] = items[0].allowed_destinations
        for item in items[1:]:
            destinations &= item.allowed_destinations

        origins: frozenset[str] = frozenset()
        for item in items:
            origins |= item.origins

        revisions = [i.policy_revision for i in items if i.policy_revision]
        return cls(
            model_scope=ModelScope.LOCAL_ONLY if local_only
            else ModelScope.CLOUD_ALLOWED,
            origins=origins,
            allowed_destinations=destinations,
            sensitive=any(item.sensitive for item in items),
            # Keep a revision only when every input agrees; a mixed merge has no
            # single classification decision behind it.
            policy_revision=revisions[0] if len(set(revisions)) == 1 else None,
        )

    def downgraded_by(self, claimed: PrivacyLabel) -> PrivacyLabel:
        """Apply a caller-supplied label without letting it loosen this one.

        Runtime §3: "a false local_only supplied by a caller cannot downgrade
        true". Narrowing is always honoured; widening is discarded.
        """
        return PrivacyLabel.merge([self, claimed])

    # ------------------------------------------------------------------ #
    # Construction and serialisation
    # ------------------------------------------------------------------ #
    @classmethod
    def for_unlabelled_import(cls) -> PrivacyLabel:
        """The default for content arriving with no classification."""
        return cls(model_scope=ModelScope.LOCAL_ONLY,
                   allowed_destinations=frozenset())

    def to_json(self) -> dict[str, Any]:
        """A stable, hashable JSON form — sets are emitted sorted."""
        return {
            "model_scope": self.model_scope.value,
            "origins": sorted(self.origins),
            "allowed_destinations": sorted(self.allowed_destinations),
            "sensitive": self.sensitive,
            "policy_revision": self.policy_revision,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> PrivacyLabel:
        """Restore a label, failing safe on anything unrecognised."""
        if not payload:
            return cls.for_unlabelled_import()
        return cls(
            model_scope=_parse_scope(payload.get("model_scope")),
            origins=_frozen(payload.get("origins")),
            allowed_destinations=_frozen(payload.get("allowed_destinations")),
            sensitive=bool(payload.get("sensitive", False)),
            policy_revision=payload.get("policy_revision"),
        )


def _parse_scope(value: Any) -> ModelScope:
    """Parse a model scope, defaulting to local-only on anything unknown.

    A corrupt row or a value written by a newer version must not silently
    become cloud-allowed.
    """
    try:
        return ModelScope(str(value))
    except ValueError:
        return ModelScope.LOCAL_ONLY
