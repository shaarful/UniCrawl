"""WHO-EIOS authenticated feed fetcher (SRS FR-07).

Pulls curated health-intelligence events from the WHO Event Information Site.
Reads EIOS_API_BASE_URL and EIOS_API_TOKEN from settings.
"""
from __future__ import annotations

from .base import FetchResult, Fetcher


class EIOSFetcher(Fetcher):
    def fetch(self, url: str) -> FetchResult:
        raise NotImplementedError
