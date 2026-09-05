"""Loop configuration — a single Pydantic settings model.

All configuration is loaded from environment variables (or a local ``.env``
file). Nothing here contains secrets — real values live in ``.env`` which is
gitignored. Copy ``config/.env.example`` to ``.env`` and fill it in.

Access the singleton via ``get_settings()``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration for every Loop component."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- General -----------------------------------------------------------
    environment: str = "development"  # development | production
    timezone: str = "Europe/Berlin"
    briefing_time: str = "08:00"      # HH:MM local time for the morning briefing
    follow_up_window_hours: int = 48  # flag emails with no reply after this

    # --- Local persistence -------------------------------------------------
    database_url: str = "sqlite:///data/loop.db"
    chroma_persist_dir: str = "data/chroma"

    # --- Telegram ----------------------------------------------------------
    telegram_bot_token: str = ""      # BotFather token
    telegram_chat_id: str = ""        # default chat id for outbound messages

    # --- Gmail / Google ----------------------------------------------------
    gmail_credentials_path: str = "credentials.json"  # OAuth client secrets
    gmail_token_path: str = "token.json"              # cached user token

    # --- Outlook / Microsoft Graph ----------------------------------------
    outlook_client_id: str = ""
    outlook_client_secret: str = ""
    outlook_tenant_id: str = "common"

    # --- Microsoft Teams ---------------------------------------------------
    teams_app_id: str = ""
    teams_app_password: str = ""

    # --- Wrike (Phase 2) ---------------------------------------------------
    wrike_api_key: str = ""

    # --- LLM: local (Ollama) ----------------------------------------------
    ollama_base_url: str = "http://localhost:11434"
    ollama_default_model: str = "llama3.1:8b"
    ollama_embed_model: str = "nomic-embed-text"

    # --- LLM: cloud fallback (Anthropic) ----------------------------------
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    # Latency (seconds) above which a local response triggers a cloud fallback.
    llm_fallback_latency_seconds: float = 10.0
    # Minimum local response length (characters) before triggering a fallback.
    llm_fallback_min_chars: int = 50

    # --- Obsidian ----------------------------------------------------------
    obsidian_vault_path: str = "~/Obsidian/Vault"
    # A second, private vault whose notes must never reach a cloud LLM.
    obsidian_private_vault_path: str = ""

    # --- Web dashboard -----------------------------------------------------
    web_host: str = "0.0.0.0"
    web_port: int = 8000

    # --- LLM routing heuristics -------------------------------------------
    # Cloud fallback triggers when a local response is shorter than this
    # (characters), contains a refusal phrase, or takes longer than the
    # latency budget below.
    local_min_response_chars: int = 40
    local_latency_budget_seconds: float = 10.0

    # --- Privacy gate ------------------------------------------------------
    # Comma-separated vault names/paths that must never reach a cloud LLM.
    private_vaults: str = ""
    # Treat all-day personal calendar events as local-only.
    private_personal_calendar: bool = True
    # Treat personal (non-group) Telegram chats as local-only.
    private_telegram_personal: bool = True

    # --- Email triage (Phase 3) -------------------------------------------
    # Comma-separated VIP sender addresses. Mail from these senders scores
    # higher on both urgency and importance.
    vip_senders: str = ""

    # --- Conversation memory (Phase 3) ------------------------------------
    # A session is considered expired after this many hours of inactivity.
    session_ttl_hours: int = 24

    # ------------------------------------------------------------------ #
    # Derived helpers
    # ------------------------------------------------------------------ #
    @property
    def vip_sender_list(self) -> list[str]:
        """Return the configured VIP senders as a normalised list."""
        return [s.strip().lower() for s in self.vip_senders.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
