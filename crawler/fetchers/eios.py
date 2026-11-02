"""WHO-EIOS authenticated feed fetcher.

Pulls articles matching an EIOS board via the service-to-service API.
Reads the EIOS_* settings (OAuth2 client credentials, board ID, paging).

Auth: OAuth2 client-credentials against Azure AD. The bearer token lasts ~1h;
it is cached per fetcher instance and refreshed on expiry or a 401.

Paging: GET /Items/matching-board/{boardId} is offset-paged (`start`, `limit`
≤ 100) and the backing Elasticsearch index refuses offsets past 10,000. The
requested time range is therefore walked in fixed windows (EIOS_WINDOW_MINUTES),
and any window reporting ≥ 10,000 matches is split in half until it fits.

Full text: the list endpoint never carries the article body. The detail
endpoint (GET /Items/{id}) defines `fullText`, but EIOS only returns it to
accounts granted full-text access — the pipeline falls back to fetching the
original article link when it is absent.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from django.conf import settings

from .base import FetchResult, HttpFetcher

log = structlog.get_logger(__name__)

# Elasticsearch `index.max_result_window`: start + limit may not exceed this.
MAX_RESULT_WINDOW = 10_000
MAX_PAGE_SIZE = 100
_MIN_WINDOW = timedelta(minutes=1)
_TOKEN_EXPIRY_MARGIN_SECONDS = 60

_BOARD_URL_RE = re.compile(r"^(?P<api>.+/api/v[^/]+)/Items/matching-board/(?P<board>[^/?#]+)")


def board_url(base_url: str, version: str, board_id: str) -> str:
    """Build the matching-board URL stored as an EIOS Source.url."""
    return f"{base_url.rstrip('/')}/api/v{version}/Items/matching-board/{board_id}"


def parse_board_url(url: str) -> tuple[str, str]:
    """Split a matching-board URL into (api_root, board_id)."""
    parts = urlsplit(url)
    m = _BOARD_URL_RE.match(urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")))
    if m is None:
        raise ValueError(f"Not an EIOS matching-board URL: {url}")
    return m.group("api"), m.group("board")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class EIOSFetcher(HttpFetcher):
    """Authenticated EIOS API client."""

    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        *,
        token_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scope: str | None = None,
        page_size: int | None = None,
        window_minutes: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.token_url = settings.EIOS_TOKEN_URL if token_url is None else token_url
        self.client_id = settings.EIOS_CLIENT_ID if client_id is None else client_id
        self.client_secret = settings.EIOS_CLIENT_SECRET if client_secret is None else client_secret
        self.scope = settings.EIOS_SCOPE if scope is None else scope
        self.page_size = min(page_size or settings.EIOS_PAGE_SIZE, MAX_PAGE_SIZE)
        self.window = timedelta(minutes=window_minutes or settings.EIOS_WINDOW_MINUTES)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._client: httpx.Client | None = None

    # --- HTTP plumbing --------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent, "Accept": "application/json"},
                transport=self._transport,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _access_token(self, *, force: bool = False) -> str:
        if not force and self._token and time.monotonic() < self._token_expires_at:
            return self._token
        if not (self.token_url and self.client_id and self.client_secret and self.scope):
            raise RuntimeError(
                "EIOS credentials missing: set EIOS_TOKEN_URL, EIOS_CLIENT_ID, "
                "EIOS_CLIENT_SECRET and EIOS_SCOPE."
            )
        response = self._http().post(
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": self.scope,
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3599))
        self._token_expires_at = time.monotonic() + expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS
        log.info("eios.token_acquired", expires_in=expires_in)
        return self._token

    def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        """GET with bearer auth; refreshes the token once on 401."""
        for attempt in (1, 2):
            token = self._access_token(force=attempt == 2)
            response = self._http().get(
                url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            if response.status_code != 401:
                return response
        return response

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        response = self._get(url, params)
        response.raise_for_status()
        return response.json()

    # --- Fetcher protocol -----------------------------------------------------

    def fetch(self, url: str) -> FetchResult:
        """Single authenticated GET — used for test-crawls of an EIOS source."""
        response = self._get(url, params={"start": 0, "limit": 1})
        return FetchResult(
            url=url,
            status_code=response.status_code,
            body=response.content,
            headers=dict(response.headers),
            final_url=str(response.url),
        )

    # --- Board articles -------------------------------------------------------

    def iter_board_items(
        self,
        url: str,
        *,
        since: datetime,
        until: datetime,
        on_window: Callable[[datetime, datetime, int], None] | None = None,
    ) -> Iterator[dict]:
        """Yield every article on the board fetched within [since, until).

        `on_window(since, until, count)` is called before each window is paged.
        """
        window_start = since
        while window_start < until:
            window_end = min(window_start + self.window, until)
            yield from self._iter_window(url, window_start, window_end, on_window)
            window_start = window_end

    def _iter_window(
        self, url: str, since: datetime, until: datetime, on_window=None,
    ) -> Iterator[dict]:
        params = {"timeSince": _iso(since), "timeUntil": _iso(until)}
        first = self._get_json(url, {**params, "start": 0, "limit": self.page_size})
        count = first.get("count") or 0

        if count >= MAX_RESULT_WINDOW and until - since > _MIN_WINDOW:
            mid = since + (until - since) / 2
            log.info("eios.window_split", since=_iso(since), until=_iso(until), count=count)
            yield from self._iter_window(url, since, mid, on_window)
            yield from self._iter_window(url, mid, until, on_window)
            return

        log.info("eios.window", since=_iso(since), until=_iso(until), count=count)
        if on_window is not None:
            on_window(since, until, count)
        page = first.get("result") or []
        start = 0
        while page:
            yield from page
            start += len(page)
            if start >= min(count, MAX_RESULT_WINDOW):
                break
            limit = min(self.page_size, MAX_RESULT_WINDOW - start)
            page = self._get_json(url, {**params, "start": start, "limit": limit})
            page = page.get("result") or []

    def get_item(self, url: str, item_id: str) -> dict | None:
        """Return article detail (may include `fullText`), or None if unavailable."""
        api_root, _ = parse_board_url(url)
        response = self._get(f"{api_root}/Items/{item_id}")
        if response.status_code != 200:
            log.warning(
                "eios.item_detail_unavailable", item_id=item_id, status=response.status_code,
            )
            return None
        return response.json()
