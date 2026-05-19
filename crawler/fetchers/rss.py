"""RSS / Atom feed fetcher.

Returns the raw feed bytes. Parsing into per-entry articles happens in the
extractor stage (`crawler.extractors`), since one feed fetch maps to many
candidate articles.
"""
from __future__ import annotations

from .base import HttpFetcher


class RSSFetcher(HttpFetcher):
    """Fetch RSS / Atom feed XML."""

    def _extra_headers(self) -> dict[str, str]:
        return {
            "Accept": (
                "application/rss+xml, application/atom+xml, "
                "application/xml;q=0.9, */*;q=0.8"
            ),
        }
