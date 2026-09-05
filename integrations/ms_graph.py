"""Shared Microsoft Graph access for the Outlook mail and calendar connectors.

**Delegated, not application, permissions.** ``docs/onboarding.md`` tells the
user to grant *delegated* ``Mail.Read``, ``Mail.Send``, and ``Calendars.Read``,
which is the right model for a personal assistant: Loop acts as you, sees only
your mailbox, and needs no tenant-wide admin consent. That is why the endpoints
below are ``/me/...`` — an application-permission token has no "me".

**Device code flow.** The delegated flow that works without a browser on the
same machine: Loop prints a short code, the user signs in anywhere, and the
token is cached. This matters because Loop is expected to run in Docker and
under a scheduler, where a redirect-to-localhost flow cannot complete.

**Why raw REST instead of msgraph-sdk.** The SDK is large, async-only, and
wraps every response in generated models. Loop needs six endpoints. ``httpx``
against the documented REST shapes is smaller, synchronous like the Google
connectors, and — more importantly — trivially testable with
``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

import httpx

from config.settings import Settings, get_settings
from core.exceptions import AuthRequiredError

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

#: Delegated scopes. Loop acts as the signed-in user, never as the tenant.
GRAPH_SCOPES: tuple[str, ...] = (
    "Mail.Read",
    "Mail.Send",
    "Calendars.Read",
)

#: Where the MSAL token cache is kept, alongside the Google token.
TOKEN_CACHE_NAME = "ms_graph_token.json"


class TokenProvider(Protocol):
    """Anything that can produce a Graph bearer token."""

    def token(self) -> str:
        ...


class DeviceCodeTokenProvider:
    """Delegated device-code auth with an on-disk token cache."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def cache_path(self) -> Path:
        # Sits next to the Google token so both live wherever the user pointed
        # GMAIL_TOKEN_PATH — usually the project root or a mounted volume.
        return Path(self.settings.gmail_token_path).expanduser().parent / TOKEN_CACHE_NAME

    def _build_app(self) -> Any:
        import msal

        cache = msal.SerializableTokenCache()
        if self.cache_path.is_file():
            try:
                cache.deserialize(self.cache_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - a corrupt cache just means re-auth
                logger.warning("Ignoring unreadable Graph token cache at %s",
                               self.cache_path)

        authority = (
            f"https://login.microsoftonline.com/"
            f"{self.settings.outlook_tenant_id or 'common'}"
        )
        app = msal.PublicClientApplication(
            self.settings.outlook_client_id,
            authority=authority,
            token_cache=cache,
        )
        return app, cache

    def _persist(self, cache: Any) -> None:
        if not cache.has_state_changed:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(cache.serialize(), encoding="utf-8")
            os.chmod(self.cache_path, 0o600)
        except Exception:  # noqa: BLE001 - caching is an optimisation
            logger.warning("Could not persist the Graph token cache")

    def token(self) -> str:
        """Return a bearer token, signing in interactively only if required."""
        if not self.settings.outlook_client_id.strip():
            raise AuthRequiredError(
                "No Outlook client id configured. Register an app in the Azure "
                "portal and set OUTLOOK_CLIENT_ID in .env."
            )

        app, cache = self._build_app()

        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(list(GRAPH_SCOPES),
                                              account=accounts[0])
            if result and "access_token" in result:
                self._persist(cache)
                return str(result["access_token"])

        from integrations.google_auth import interactive_possible

        if not interactive_possible():
            raise AuthRequiredError(
                "Microsoft sign-in required but this environment is not "
                f"interactive. Run `loop status` once on your own machine to "
                f"create {self.cache_path}, then copy it here."
            )

        flow = app.initiate_device_flow(scopes=list(GRAPH_SCOPES))
        if "user_code" not in flow:
            raise AuthRequiredError(
                f"Could not start Microsoft device sign-in: {flow.get('error_description', flow)}"
            )
        # Printed rather than logged: this is a prompt the user must act on.
        print(flow["message"], flush=True)  # noqa: T201

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise AuthRequiredError(
                f"Microsoft sign-in failed: {result.get('error_description', result)}"
            )
        self._persist(cache)
        return str(result["access_token"])


class GraphClient:
    """Minimal synchronous Microsoft Graph REST client."""

    BASE_URL = GRAPH_BASE_URL

    def __init__(self, settings: Settings | None = None,
                 token_provider: TokenProvider | None = None,
                 transport: Any | None = None) -> None:
        self.settings = settings or get_settings()
        self._token_provider = token_provider
        # Injectable for tests (httpx.MockTransport); never set in production.
        self._transport = transport
        self._http: httpx.Client | None = None

    @property
    def configured(self) -> bool:
        """True when an Azure app registration is configured."""
        if self._token_provider is not None:
            return True
        return bool(self.settings.outlook_client_id.strip())

    def _get_token_provider(self) -> TokenProvider:
        if self._token_provider is None:
            self._token_provider = DeviceCodeTokenProvider(self.settings)
        return self._token_provider

    def _client(self) -> httpx.Client:
        if self._http is None:
            kwargs: dict[str, Any] = {"base_url": self.BASE_URL, "timeout": 30.0}
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._http = httpx.Client(**kwargs)
        return self._http

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def get(self, path: str, **params: Any) -> dict:
        """GET a Graph endpoint and return the decoded JSON body."""
        if not self.configured:
            raise AuthRequiredError(
                "Outlook is not configured. Set OUTLOOK_CLIENT_ID in .env."
            )
        token = self._get_token_provider().token()
        response = self._client().get(
            path, params=params or None,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: dict) -> httpx.Response:
        """POST JSON to a Graph endpoint."""
        if not self.configured:
            raise AuthRequiredError(
                "Outlook is not configured. Set OUTLOOK_CLIENT_ID in .env."
            )
        token = self._get_token_provider().token()
        response = self._client().post(
            path,
            content=json.dumps(payload),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        return response
