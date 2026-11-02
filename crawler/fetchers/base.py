"""Fetcher interface and shared HTTP implementation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass
class FetchResult:
    """Raw fetched payload before extraction."""

    url: str
    status_code: int
    body: bytes
    headers: dict[str, str]
    final_url: str


class Fetcher(Protocol):
    """All fetchers implement this protocol."""

    def fetch(self, url: str) -> FetchResult:  # pragma: no cover
        ...


class HttpFetcher:
    """Shared HTTP fetch logic for HTML and RSS sources.

    Network errors (timeout, connection refused, DNS failure) propagate as
    httpx.HTTPError subclasses; the scheduler catches them to trigger the
    retry back-off. Non-2xx responses are returned as-is — the pipeline
    decides whether to treat them as failures based on status_code.

    Subclasses override `_extra_headers()` to add content-type-specific headers
    (e.g. Accept). Tests pass `transport=httpx.MockTransport(...)` to bypass
    the network.
    """

    USER_AGENT = "UniCrawl/0.1 (+disease surveillance crawler)"
    DEFAULT_TIMEOUT = 30.0

    def __init__(
        self,
        *,
        timeout: float | None = None,
        user_agent: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.timeout = timeout if timeout is not None else self.DEFAULT_TIMEOUT
        self.user_agent = user_agent or self.USER_AGENT
        self._transport = transport

    def _extra_headers(self) -> dict[str, str]:
        """Hook for subclasses to add content-type-specific request headers."""
        return {}

    def fetch(self, url: str) -> FetchResult:
        headers = {"User-Agent": self.user_agent, **self._extra_headers()}
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers=headers,
            transport=self._transport,
        ) as client:
            response = client.get(url)
        return FetchResult(
            url=url,
            status_code=response.status_code,
            body=response.content,
            headers=dict(response.headers),
            final_url=str(response.url),
        )
