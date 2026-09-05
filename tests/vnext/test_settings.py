"""vNext deployment settings (interfaces contract §7)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from loop.core.settings import Settings


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Safe defaults
# --------------------------------------------------------------------------- #
def test_no_vault_is_selected_by_default():
    """interfaces §7: explicit selection is required before vault writes."""
    assert _settings().vault_root is None


def test_no_model_is_guessed_on_a_fresh_install():
    settings = _settings()
    assert settings.ollama_default_model == ""
    assert settings.ollama_embed_model == ""


def test_cloud_is_off_by_default():
    assert _settings().cloud_enabled is False
    assert _settings().cloud_available is False


# --------------------------------------------------------------------------- #
# Cloud gating
# --------------------------------------------------------------------------- #
def test_cloud_needs_flag_key_model_and_budget():
    ready = _settings(cloud_enabled=True, anthropic_api_key="k",
                      anthropic_model="claude-x", cloud_daily_budget_usd=5)
    assert ready.cloud_available is True


def test_zero_budget_keeps_cloud_unavailable_even_when_enabled():
    """A positive budget is required to enable paid fallback."""
    settings = _settings(cloud_enabled=True, anthropic_api_key="k",
                         anthropic_model="claude-x", cloud_daily_budget_usd=0)
    assert settings.cloud_available is False


def test_missing_model_keeps_cloud_unavailable():
    settings = _settings(cloud_enabled=True, anthropic_api_key="k",
                         cloud_daily_budget_usd=5)
    assert settings.cloud_available is False


def test_key_without_the_flag_does_not_enable_cloud():
    settings = _settings(anthropic_api_key="k", anthropic_model="m",
                         cloud_daily_budget_usd=5)
    assert settings.cloud_available is False


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def test_invalid_timezone_is_rejected():
    with pytest.raises(ValidationError):
        _settings(timezone="Mars/Olympus")


def test_valid_iana_timezone_is_accepted():
    assert _settings(timezone="America/New_York").timezone == "America/New_York"


def test_invalid_environment_is_rejected():
    with pytest.raises(ValidationError):
        _settings(environment="staging")


def test_invalid_vault_backend_is_rejected():
    with pytest.raises(ValidationError):
        _settings(vault_backend="magic")


def test_scriptorium_backend_is_allowed():
    assert _settings(vault_backend="scriptorium").vault_backend == "scriptorium"


# --------------------------------------------------------------------------- #
# Derived paths
# --------------------------------------------------------------------------- #
def test_capability_paths_parse_from_json():
    roots = _settings(capability_paths='["a", "b"]').capability_roots
    assert len(roots) == 2
    assert all(root.is_absolute() for root in roots)


def test_a_bare_capability_path_still_works():
    assert len(_settings(capability_paths="just-one").capability_roots) == 1


def test_data_dir_resolves_absolute(tmp_path):
    assert _settings(data_dir=str(tmp_path)).data_path.is_absolute()


def test_vault_root_resolves_when_configured(tmp_path):
    assert _settings(obsidian_vault_path=str(tmp_path)).vault_root == tmp_path.resolve()


# --------------------------------------------------------------------------- #
# Compatibility
# --------------------------------------------------------------------------- #
def test_deprecated_outlook_secret_is_accepted_but_warns(tmp_path):
    """interfaces §9 keeps it as an ignored compatibility key."""
    settings = _settings(outlook_client_secret="legacy")
    with pytest.warns(DeprecationWarning):
        assert settings.outlook_client_secret == "legacy"


def test_unknown_env_keys_do_not_break_startup():
    assert _settings(SOMETHING_UNRELATED="x").environment == "development"
