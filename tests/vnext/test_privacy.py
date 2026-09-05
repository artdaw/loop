"""PrivacyLabel algebra (runtime contract §3).

The merge rules are deliberately asymmetric: restrictions spread, permissions
shrink. `local_only` and `sensitive` OR together, origins union, and allowed
destinations *intersect* — so combining two inputs can never produce a label
that permits more than either input did alone.
"""

from __future__ import annotations

import pytest

from loop.core.privacy import ModelScope, PrivacyLabel


def _label(**kw) -> PrivacyLabel:
    return PrivacyLabel(**kw)


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #
def test_unlabelled_import_defaults_to_local_only_with_no_destination():
    """Runtime §3: unlabelled imported content defaults to the safe end."""
    label = PrivacyLabel.for_unlabelled_import()

    assert label.model_scope is ModelScope.LOCAL_ONLY
    assert label.allowed_destinations == frozenset()
    assert label.is_local_only is True


def test_empty_destination_set_permits_local_use_only():
    assert _label().permits_destination("owner:telegram") is False


def test_a_named_destination_is_permitted_when_listed():
    label = _label(allowed_destinations={"owner:telegram"})
    assert label.permits_destination("owner:telegram") is True
    assert label.permits_destination("owner:email") is False


# --------------------------------------------------------------------------- #
# Merge algebra
# --------------------------------------------------------------------------- #
def test_local_only_wins_over_cloud_allowed():
    cloud = _label(model_scope=ModelScope.CLOUD_ALLOWED)
    local = _label(model_scope=ModelScope.LOCAL_ONLY)

    assert PrivacyLabel.merge([cloud, local]).is_local_only is True
    assert PrivacyLabel.merge([local, cloud]).is_local_only is True


def test_cloud_allowed_survives_only_when_every_input_allows_it():
    a = _label(model_scope=ModelScope.CLOUD_ALLOWED)
    b = _label(model_scope=ModelScope.CLOUD_ALLOWED)
    assert PrivacyLabel.merge([a, b]).is_local_only is False


def test_sensitive_ors():
    plain = _label(sensitive=False)
    secret = _label(sensitive=True)
    assert PrivacyLabel.merge([plain, secret]).sensitive is True


def test_origins_union():
    a = _label(origins={"telegram_private"})
    b = _label(origins={"vault"})
    assert PrivacyLabel.merge([a, b]).origins == frozenset(
        {"telegram_private", "vault"})


def test_destinations_intersect():
    """Permissions shrink: only destinations *both* inputs allow survive."""
    a = _label(allowed_destinations={"owner:telegram", "owner:email"})
    b = _label(allowed_destinations={"owner:telegram"})

    assert PrivacyLabel.merge([a, b]).allowed_destinations == frozenset(
        {"owner:telegram"})


def test_disjoint_destinations_merge_to_nothing():
    a = _label(allowed_destinations={"owner:telegram"})
    b = _label(allowed_destinations={"owner:email"})
    assert PrivacyLabel.merge([a, b]).allowed_destinations == frozenset()


def test_merging_nothing_returns_the_safe_default():
    merged = PrivacyLabel.merge([])
    assert merged.is_local_only is True
    assert merged.allowed_destinations == frozenset()


def test_merging_one_label_returns_an_equal_label():
    label = _label(model_scope=ModelScope.CLOUD_ALLOWED,
                   origins={"vault"}, allowed_destinations={"owner:telegram"})
    assert PrivacyLabel.merge([label]) == label


def test_merge_is_order_independent():
    a = _label(model_scope=ModelScope.CLOUD_ALLOWED, origins={"a"},
               allowed_destinations={"x", "y"})
    b = _label(sensitive=True, origins={"b"}, allowed_destinations={"y"})
    assert PrivacyLabel.merge([a, b]) == PrivacyLabel.merge([b, a])


def test_merge_can_never_widen_beyond_an_input():
    """The property that makes the algebra safe."""
    a = _label(model_scope=ModelScope.CLOUD_ALLOWED,
               allowed_destinations={"owner:telegram"})
    b = _label(model_scope=ModelScope.LOCAL_ONLY, allowed_destinations=set())

    merged = PrivacyLabel.merge([a, b])

    assert merged.is_local_only is True
    assert merged.allowed_destinations <= a.allowed_destinations
    assert merged.allowed_destinations <= b.allowed_destinations


# --------------------------------------------------------------------------- #
# No downgrade
# --------------------------------------------------------------------------- #
def test_a_caller_cannot_downgrade_a_local_only_label():
    """Runtime §3: 'a false local_only supplied by a caller cannot downgrade true'."""
    truth = _label(model_scope=ModelScope.LOCAL_ONLY)
    claimed = _label(model_scope=ModelScope.CLOUD_ALLOWED)

    assert truth.downgraded_by(claimed).is_local_only is True


def test_narrowing_to_local_only_is_always_allowed():
    label = _label(model_scope=ModelScope.CLOUD_ALLOWED)
    assert label.downgraded_by(_label(model_scope=ModelScope.LOCAL_ONLY)).is_local_only


def test_labels_are_immutable():
    label = _label()
    with pytest.raises((AttributeError, TypeError)):
        label.sensitive = True  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #
def test_round_trips_through_json():
    label = _label(model_scope=ModelScope.CLOUD_ALLOWED, origins={"vault"},
                   allowed_destinations={"owner:telegram"}, sensitive=True,
                   policy_revision="privacy-v1")

    assert PrivacyLabel.from_json(label.to_json()) == label


def test_json_uses_sorted_lists_for_stable_hashing():
    label = _label(origins={"z", "a"}, allowed_destinations={"y", "b"})
    payload = label.to_json()

    assert payload["origins"] == ["a", "z"]
    assert payload["allowed_destinations"] == ["b", "y"]


def test_unknown_json_scope_falls_back_to_local_only():
    """A corrupt or future value must fail safe, not raise or default open."""
    restored = PrivacyLabel.from_json({"model_scope": "something_new"})
    assert restored.is_local_only is True
