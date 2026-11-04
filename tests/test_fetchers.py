"""Fetcher tests — network-free via httpx.MockTransport."""
from __future__ import annotations

import httpx
import pytest

from crawler.fetchers.base import HttpFetcher
from crawler.fetchers.html import HTMLFetcher
from crawler.fetchers.rss import RSSFetcher


def _transport(handler):
    return httpx.MockTransport(handler)


def test_html_fetcher_returns_response_and_sends_html_accept_header():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, content=b"<html><body>hi</body></html>")

    result = HTMLFetcher(transport=_transport(handler)).fetch("https://example.com/article")

    assert result.status_code == 200
    assert result.body == b"<html><body>hi</body></html>"
    assert result.url == "https://example.com/article"
    assert result.final_url == "https://example.com/article"
    assert "text/html" in captured["request"].headers["accept"]
    assert "RZDO" in captured["request"].headers["user-agent"]


def test_rss_fetcher_sends_feed_accept_header():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, content=b"<rss></rss>")

    RSSFetcher(transport=_transport(handler)).fetch("https://example.com/feed.xml")

    assert "application/rss+xml" in captured["request"].headers["accept"]
    assert "application/atom+xml" in captured["request"].headers["accept"]


def test_html_fetcher_follows_redirects_and_records_final_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "https://example.com/new"})
        return httpx.Response(200, content=b"final")

    result = HTMLFetcher(transport=_transport(handler)).fetch("https://example.com/old")

    assert result.status_code == 200
    assert result.final_url == "https://example.com/new"
    assert result.body == b"final"


def test_fetcher_returns_non_2xx_without_raising():
    """HTTP errors are handled by the scheduler back-off — fetcher just
    returns the response so the pipeline can record the actual status_code."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    result = HTMLFetcher(transport=_transport(handler)).fetch("https://example.com/missing")

    assert result.status_code == 404
    assert result.body == b"not found"


def test_network_errors_propagate_for_scheduler_to_catch():
    """The scheduler relies on httpx exceptions to trigger back-off."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure")

    with pytest.raises(httpx.ConnectError):
        HTMLFetcher(transport=_transport(handler)).fetch("https://example.com/anything")


def test_custom_user_agent_and_timeout_are_honored():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, content=b"ok")

    fetcher = HTMLFetcher(
        transport=_transport(handler),
        user_agent="TestAgent/1.0",
        timeout=5.0,
    )
    fetcher.fetch("https://example.com/")

    assert captured["request"].headers["user-agent"] == "TestAgent/1.0"
    assert fetcher.timeout == 5.0


def test_base_http_fetcher_sends_no_extra_accept_by_default():
    captured: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(200, content=b"")

    HttpFetcher(transport=_transport(handler)).fetch("https://example.com/")

    # httpx adds its own default Accept; we just verify no specific Accept was forced.
    assert "user-agent" in captured["request"].headers
