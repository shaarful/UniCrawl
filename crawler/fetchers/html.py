"""HTML fetcher for news portals and government bulletins."""
from __future__ import annotations

from .base import HttpFetcher


class HTMLFetcher(HttpFetcher):
    """Fetch HTML pages over HTTP/HTTPS."""

    def _extra_headers(self) -> dict[str, str]:
        return {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"}
