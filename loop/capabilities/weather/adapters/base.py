"""The adapter contract and its HTTP transport (weather §2, §3).

Transport is separated from normalization on purpose: parsing is pure and
testable against recorded payloads, and the only part that touches the network
is this file. A parser that fetches cannot be tested without a network, and a
test that needs a network stops being run.

Every request is bounded — connect and read timeouts, a response size ceiling,
and a caller-supplied budget. An adapter that can hang has no timeout; an
adapter that can be handed a 500 MB body has no ceiling.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from loop.capabilities.weather.normalize import WeatherSample
from loop.capabilities.weather.sources import SourceDescriptor

logger = logging.getLogger(__name__)

#: Bounded by default. Overridable per adapter, never unbounded.
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class AdapterError(RuntimeError):
    """A provider failed. Carries the source id so the bundle can name it."""

    def __init__(self, source_id: str, message: str) -> None:
        super().__init__(f"{source_id}: {message}")
        self.source_id = source_id
        self.message = message


@dataclass
class HttpResponse:
    status_code: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpTransport:
    """A bounded HTTP GET. Injected, so tests never need a network."""

    def __init__(self, *, connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
                 read_timeout: float = READ_TIMEOUT_SECONDS,
                 max_bytes: int = MAX_RESPONSE_BYTES,
                 user_agent: str = "loop/0.1 (personal assistant)") -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    def get(self, url: str, *, params: dict[str, Any] | None = None
            ) -> HttpResponse:
        # Imported here so that merely importing the adapter module does not
        # require the HTTP client, and a missing optional dependency cannot
        # break unrelated commands (CLAUDE.md: load optional providers lazily).
        import httpx

        try:
            with httpx.Client(
                    timeout=httpx.Timeout(self.read_timeout,
                                          connect=self.connect_timeout),
                    headers={"User-Agent": self.user_agent},
                    follow_redirects=True) as client, \
                    client.stream("GET", url, params=params) as response:
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise AdapterError(
                            url, f"response exceeded {self.max_bytes} bytes")
                    chunks.append(chunk)
                return HttpResponse(response.status_code, b"".join(chunks),
                                    dict(response.headers))
        except AdapterError:
            raise
        except Exception as exc:                       # noqa: BLE001 — reported
            raise AdapterError(url, f"{exc.__class__.__name__}: {exc}") from exc


class ProviderAdapter(Protocol):
    """What every weather source adapter provides."""

    @property
    def descriptor(self) -> SourceDescriptor:
        """Validated metadata for selection and provenance."""

    def fetch(self, *, latitude: float, longitude: float, start: int, end: int,
              variables: list[str], now: int) -> list[WeatherSample]:
        """Fetch and normalize. Raises AdapterError on any provider failure."""
