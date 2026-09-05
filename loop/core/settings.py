"""Deployment configuration (interfaces contract §7).

One Pydantic Settings object owns *deployment* configuration. Personal
behaviour — time interpretation, routines, notification policy — lives in vault
policy under ``_ctx/loop/``, not here. SQLite stores the validated policy
revision, never a second editable set of competing rules.

Separating the two matters because they have different authorities and change
at different rates: an operator edits ``.env`` and restarts, while the owner
edits behaviour in their vault and expects it to take effect without a deploy.

Distinct from the legacy :mod:`config.settings`, which stays in place while the
Phase 4 modules are migrated.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment/deployment configuration for Loop vNext."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core -------------------------------------------------------------
    environment: str = "development"          # development | production | test
    timezone: str = "Europe/Berlin"           # initial owner-timezone default
    database_url: str = "sqlite:///data/loop.db"
    data_dir: str = "data"

    # --- Capability discovery --------------------------------------------
    capability_paths: str = '["capabilities", "data/capabilities"]'

    # --- Vault ------------------------------------------------------------
    # Empty by default: interfaces §7 requires an explicit selection before any
    # vault write, so a fresh install cannot touch a guessed directory.
    obsidian_vault_path: str = ""
    obsidian_private_vault_path: str = ""
    vault_backend: str = "builtin"            # builtin | scriptorium
    scriptorium_command: str = ""             # explicit argv, never shell eval
    loop_policy_path: str = "_ctx/loop/manifest.yaml"

    # --- Models -----------------------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_default_model: str = ""            # pinned during init, not guessed
    ollama_embed_model: str = ""
    cloud_enabled: bool = False
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    cloud_daily_budget_usd: float = 0.0
    chroma_persist_dir: str = "data/chroma"

    # --- Channels and connectors -----------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_user_id: str = ""
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"
    outlook_client_id: str = ""
    outlook_tenant_id: str = "common"

    # --- Deprecated compatibility ----------------------------------------
    # Retained as an ignored key so an existing .env does not fail validation;
    # delegated device-code auth uses no secret (interfaces §9).
    outlook_client_secret: str = Field(default="", deprecated=True)

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Not a valid IANA timezone: {value!r}") from exc
        return value

    @field_validator("environment")
    @classmethod
    def _valid_environment(cls, value: str) -> str:
        allowed = {"development", "production", "test"}
        if value not in allowed:
            raise ValueError(f"environment must be one of {sorted(allowed)}")
        return value

    @field_validator("vault_backend")
    @classmethod
    def _valid_backend(cls, value: str) -> str:
        allowed = {"builtin", "scriptorium"}
        if value not in allowed:
            raise ValueError(f"vault_backend must be one of {sorted(allowed)}")
        return value

    # ------------------------------------------------------------------ #
    # Derived
    # ------------------------------------------------------------------ #
    @property
    def data_path(self) -> Path:
        """DATA_DIR resolved to an absolute path at startup."""
        return Path(self.data_dir).expanduser().resolve()

    @property
    def capability_roots(self) -> list[Path]:
        """Confined discovery roots, resolved absolute."""
        try:
            raw = json.loads(self.capability_paths)
        except json.JSONDecodeError:
            raw = [self.capability_paths]
        if not isinstance(raw, list):
            raw = [raw]
        return [Path(str(p)).expanduser().resolve() for p in raw]

    @property
    def vault_root(self) -> Path | None:
        """The selected vault root, or None when no vault is configured."""
        text = self.obsidian_vault_path.strip()
        return Path(text).expanduser().resolve() if text else None

    @property
    def cloud_available(self) -> bool:
        """Cloud use needs the flag, a key, a model *and* a positive budget.

        interfaces §7: a positive budget is required to enable paid fallback, so
        an enabled flag with a zero budget stays off rather than spending.
        """
        return bool(self.cloud_enabled and self.anthropic_api_key.strip()
                    and self.anthropic_model.strip()
                    and self.cloud_daily_budget_usd > 0)


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
