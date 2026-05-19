"""Social-media API fetcher (placeholder).

Per-platform implementations to be added; respect each platform's API ToS and
rate limits. SRS §2.5 forbids anti-bot circumvention — only authenticated APIs.
"""
from __future__ import annotations

from .base import FetchResult, Fetcher


class SocialFetcher(Fetcher):
    def fetch(self, url: str) -> FetchResult:
        raise NotImplementedError
