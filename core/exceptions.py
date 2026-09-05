"""Loop custom exceptions.

Centralised exception types so callers can catch specific failure modes
without depending on implementation details of individual modules.
"""

from __future__ import annotations


class LoopError(Exception):
    """Base class for all Loop-specific errors."""


class PrivacyError(LoopError):
    """Raised when a request that must stay local cannot be served locally.

    The privacy gate raises this instead of silently falling back to a cloud
    LLM. For example: an Obsidian *private* vault note, a personal calendar
    event, or a private Telegram message must be processed by the local Ollama
    model only — if Ollama is unavailable, we fail closed with a PrivacyError
    rather than leak the data to Anthropic.
    """


class BackendUnavailableError(LoopError):
    """Raised when a required LLM backend cannot be reached."""
