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


class ApprovalRequiredError(LoopError):
    """Raised when an action needs explicit approval that was not given.

    The autonomy gate's counterpart to :class:`PrivacyError`. Where the privacy
    gate fails *closed* (never escalating to the cloud), the autonomy gate fails
    to *asking*: it refuses to act unattended, but the user can always approve.
    """


class WrikeNotConfiguredError(LoopError):
    """Raised when a Wrike API call is attempted without an API key configured."""


class AuthRequiredError(LoopError):
    """Raised when an integration needs a sign-in that cannot be done right now.

    Distinct from a missing configuration: the credentials are present but no
    valid token exists and the environment cannot run an interactive flow (a
    container, a cron job, a background scheduler). The message tells the user
    which command to run on a machine with a browser.
    """
