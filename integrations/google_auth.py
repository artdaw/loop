"""Shared Google OAuth for the Gmail and Google Calendar connectors.

Both connectors authenticate as the same person against the same Google
account, so they share one client-secrets file and one cached token.

**Why the scopes are unioned here rather than per-connector.** A cached token
carries the scopes it was granted. If Gmail minted a token for
``gmail.readonly + gmail.send`` and Calendar later asked for
``calendar.readonly``, the cached token would not satisfy Calendar — and
re-running the flow for the narrower scope would *drop* Gmail's access. One
union scope set, requested once, avoids that whole class of bug. Widening
:data:`GOOGLE_SCOPES` invalidates existing tokens by design: the user is asked
to re-consent, which is the correct behaviour when Loop wants more access.

**Headless environments.** The desktop OAuth flow needs a browser. Inside Docker
or a scheduler there is none, so :meth:`GoogleAuth.credentials` refuses to hang
and raises :class:`~core.exceptions.AuthRequiredError` telling the user to run
``loop status`` once on their own machine to mint the token.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

from config.settings import Settings, get_settings
from core.exceptions import AuthRequiredError

logger = logging.getLogger(__name__)

#: Every scope Loop needs from Google, requested as one set. See module docs.
GOOGLE_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
)


def interactive_possible() -> bool:
    """True when an OAuth consent flow could plausibly reach a browser.

    Docker sets no DISPLAY and has no TTY; schedulers have neither. Guessing
    wrong in the permissive direction means a background job blocks forever on a
    consent URL nobody will ever see, so this errs toward "no".
    """
    if os.environ.get("LOOP_FORCE_INTERACTIVE_AUTH") == "1":
        return True
    if os.environ.get("LOOP_NON_INTERACTIVE") == "1":
        return False
    # Running inside a container: /.dockerenv is present in Docker images.
    if Path("/.dockerenv").exists():
        return False
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


class GoogleAuth:
    """Loads, refreshes, and (when possible) mints Google OAuth credentials."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------ #
    # Paths
    # ------------------------------------------------------------------ #
    @property
    def client_secrets_path(self) -> Path:
        return Path(self.settings.gmail_credentials_path).expanduser()

    @property
    def token_path(self) -> Path:
        return Path(self.settings.gmail_token_path).expanduser()

    @property
    def configured(self) -> bool:
        """True when the client-secrets file exists."""
        return self.client_secrets_path.is_file()

    # ------------------------------------------------------------------ #
    # Credentials
    # ------------------------------------------------------------------ #
    def credentials(self, *, allow_interactive: bool = True) -> Any:
        """Return valid Google credentials, refreshing or minting as needed."""
        if not self.configured:
            raise AuthRequiredError(
                f"Google client secrets not found at {self.client_secrets_path}. "
                "Create an OAuth 'Desktop app' client in Google Cloud Console, "
                "download the JSON, and point GMAIL_CREDENTIALS_PATH at it."
            )

        creds = self._load_cached()

        if creds is not None and self._is_valid(creds):
            return creds

        if creds is not None and self._can_refresh(creds):
            try:
                self._refresh(creds)
                self._save(creds)
                return creds
            except Exception as exc:  # noqa: BLE001 - fall through to re-consent
                logger.warning("Google token refresh failed (%s); re-authorising", exc)

        if not (allow_interactive and interactive_possible()):
            raise AuthRequiredError(
                "Google sign-in required but this environment has no browser. "
                "Run `loop status` (or any Google command) once on your own "
                f"machine to create {self.token_path}, then copy it here."
            )

        creds = self._run_flow()
        self._save(creds)
        return creds

    # ------------------------------------------------------------------ #
    # Seams — overridden in tests so no real OAuth is ever performed
    # ------------------------------------------------------------------ #
    def _load_cached(self) -> Any | None:
        """Load the cached token, or ``None`` when absent/unreadable."""
        if not self.token_path.is_file():
            return None
        try:
            from google.oauth2.credentials import Credentials

            return Credentials.from_authorized_user_file(
                str(self.token_path), list(GOOGLE_SCOPES)
            )
        except Exception as exc:  # noqa: BLE001 - a corrupt token is recoverable
            logger.warning("Ignoring unreadable Google token at %s: %s",
                           self.token_path, exc)
            return None

    @staticmethod
    def _is_valid(creds: Any) -> bool:
        """Valid *and* carrying every scope Loop needs."""
        if not getattr(creds, "valid", False):
            return False
        has_scopes = getattr(creds, "has_scopes", None)
        if callable(has_scopes):
            return bool(has_scopes(list(GOOGLE_SCOPES)))
        return True

    @staticmethod
    def _can_refresh(creds: Any) -> bool:
        return bool(getattr(creds, "expired", False)
                    and getattr(creds, "refresh_token", None))

    @staticmethod
    def _refresh(creds: Any) -> None:
        from google.auth.transport.requests import Request

        creds.refresh(Request())

    def _run_flow(self) -> Any:
        """Run the interactive desktop consent flow."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.client_secrets_path), list(GOOGLE_SCOPES)
        )
        # port=0 lets the OS pick a free port for the loopback redirect.
        return flow.run_local_server(port=0)

    def _save(self, creds: Any) -> None:
        """Persist the token, readable only by the current user."""
        try:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            payload = creds.to_json()
            self.token_path.write_text(payload, encoding="utf-8")
            # The token grants mailbox access; keep it off other users' eyes.
            os.chmod(self.token_path, 0o600)
        except Exception:  # noqa: BLE001 - failing to cache must not break the call
            logger.warning("Could not cache the Google token at %s", self.token_path)

    # ------------------------------------------------------------------ #
    # Service construction
    # ------------------------------------------------------------------ #
    def build(self, api: str, version: str, *, allow_interactive: bool = True) -> Any:
        """Build a Google API service client (e.g. ``build("gmail", "v1")``)."""
        from googleapiclient.discovery import build as google_build

        creds = self.credentials(allow_interactive=allow_interactive)
        # cache_discovery=False avoids a noisy oauth2client warning and a
        # writable-cache requirement inside containers.
        return google_build(api, version, credentials=creds, cache_discovery=False)
