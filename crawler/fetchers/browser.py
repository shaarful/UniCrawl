"""Render JavaScript-built pages in a headless browser, only when a page needs it.

Many sites send an empty shell and build the page with JavaScript, so a plain HTTP
request sees no text. A browser is slow and heavy, so PageLoader makes the plain
request first and renders only when the response is clearly such a shell.

The browser identifies itself with the crawler's normal user agent and makes no
attempt to look like a person: sites that refuse the crawler are not worked around.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import structlog
from bs4 import BeautifulSoup
from django.conf import settings

from .base import FetchResult

log = structlog.get_logger(__name__)

# A shell has scripts but almost no text or links of its own.
_SHELL_MAX_TEXT_CHARS = 200
_SHELL_MAX_LINKS = 5

# Not needed to read the text, and they make up most of a page's download.
_BLOCKED_RESOURCES = frozenset({"image", "media", "font"})

# Waiting this long for the page to go quiet; analytics can keep some pages busy forever.
_NETWORK_IDLE_TIMEOUT_MS = 10_000

_INSTALL_HINT = "run: pipenv run playwright install chromium"


class BrowserUnavailable(Exception):
    """The browser cannot start here, so no page can be rendered."""


class RenderFailed(Exception):
    """This page could not be rendered; other pages may still work."""


def needs_rendering(html: bytes) -> bool:
    """Whether this HTML is an empty shell that JavaScript fills in."""
    soup = BeautifulSoup(html, "lxml")
    if not soup.find("script"):
        return False
    if len(soup.find_all("a", href=True)) >= _SHELL_MAX_LINKS:
        return False
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    body = soup.body or soup
    return len(body.get_text(" ", strip=True)) < _SHELL_MAX_TEXT_CHARS


class BrowserRenderer:
    """A headless Chromium, started on first use because most crawls never need it.

    Everything Playwright does runs on one dedicated thread. Its sync API must stay on
    the thread that started it, and it keeps an event loop running there, which makes
    Django refuse database calls on that thread (SynchronousOnlyOperation). So the
    crawl's own thread, which does use the database, must never touch Playwright.
    """

    def __init__(self, user_agent: str) -> None:
        self._user_agent = user_agent
        self._playwright = None
        self._browser = None
        self._context = None
        self._thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crawler-browser")

    def _start(self) -> None:
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(f"Playwright is not installed ({_INSTALL_HINT})") from exc
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch()
            self._context = self._browser.new_context(user_agent=self._user_agent)
            self._context.route("**/*", self._filter_request)
        except Error as exc:
            self._stop()
            raise BrowserUnavailable(f"{exc} ({_INSTALL_HINT})") from exc

    @staticmethod
    def _filter_request(route) -> None:
        if route.request.resource_type in _BLOCKED_RESOURCES:
            route.abort()
        else:
            route.continue_()

    def render(self, url: str) -> FetchResult:
        return self._thread.submit(self._render, url).result()

    def close(self) -> None:
        self._thread.submit(self._stop).result()
        self._thread.shutdown()

    def _render(self, url: str) -> FetchResult:
        if self._context is None:
            self._start()

        from playwright.sync_api import Error
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        page = self._context.new_page()
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=settings.CRAWLER_BROWSER_TIMEOUT_SECONDS * 1000,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=_NETWORK_IDLE_TIMEOUT_MS)
            except PlaywrightTimeout:
                pass  # still busy, but what has loaded so far is usually enough
            return FetchResult(
                url=url,
                status_code=response.status if response else 200,
                body=page.content().encode("utf-8"),
                headers=dict(response.headers) if response else {},
                final_url=page.url,
            )
        except Error as exc:
            raise RenderFailed(f"{type(exc).__name__}: {exc}") from exc
        finally:
            page.close()

    def _stop(self) -> None:
        for resource, stop in ((self._browser, "close"), (self._playwright, "stop")):
            if resource is not None:
                try:
                    getattr(resource, stop)()
                except Exception as exc:  # shutting down must never fail a crawl
                    log.warning("browser.close_failed", error=str(exc))
        self._playwright = self._browser = self._context = None


class PageLoader:
    """Loads pages with `fetcher`, rendering only those that are empty shells."""

    def __init__(self, fetcher, *, on_warning: Callable[[str], None] | None = None) -> None:
        self._fetcher = fetcher
        self._on_warning = on_warning or (lambda message: None)
        self._renderer: BrowserRenderer | None = None
        self._browser_usable = settings.CRAWLER_BROWSER_ENABLED
        self._site_needs_browser = False

    def load(self, url: str) -> FetchResult:
        """Raises what the fetcher raises; a failed render falls back to the plain response."""
        result = self._fetcher.fetch(url)
        if result.status_code >= 400:
            return result
        # One shell means the whole site is built this way, so stop checking each page.
        if self._site_needs_browser or needs_rendering(result.body):
            self._site_needs_browser = True
            return self.render(url) or result
        return result

    def render(self, url: str) -> FetchResult | None:
        """The rendered page, or None if it could not be rendered (a warning is reported)."""
        if not self._browser_usable:
            return None
        if self._renderer is None:
            self._renderer = BrowserRenderer(self._fetcher.user_agent)
        try:
            return self._renderer.render(url)
        except BrowserUnavailable as exc:
            self._browser_usable = False
            log.error("browser.unavailable", error=str(exc))
            self._on_warning(f"This site needs a browser, but it could not start: {exc}")
        except RenderFailed as exc:
            log.warning("browser.render_failed", url=url, error=str(exc))
            self._on_warning(f"Could not render {url}: {exc}")
        return None

    def close(self) -> None:
        if self._renderer is not None:
            self._renderer.close()
            self._renderer = None
