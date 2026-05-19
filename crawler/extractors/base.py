"""Main-text extraction from fetched HTML pages and RSS/Atom feeds.

HTML: trafilatura strips boilerplate and returns the article body; falls back to
a `<title>` tag scrape when trafilatura yields no title.

RSS/Atom: feedparser handles both formats. Each entry's `<content>` is preferred
over `<summary>`/`<description>`. HTML inside feed payloads is stripped to text.

Language detection is intentionally left to the pipeline (langdetect on the
extracted body), to keep extraction deterministic and side-effect-free.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import feedparser
import trafilatura
from bs4 import BeautifulSoup


@dataclass
class ExtractedContent:
    title: str | None
    body: str
    published_at: str | None  # raw string from source; pipeline parses to datetime
    language: str | None


@dataclass
class FeedItem:
    """One entry parsed from an RSS/Atom feed, with its own URL."""

    url: str
    content: ExtractedContent


# --- HTML ---------------------------------------------------------------------

def extract_html(html: bytes, url: str) -> ExtractedContent:
    """Extract title + main body text from an HTML page."""
    body = trafilatura.extract(
        html,
        url=url,
        favor_recall=True,
        include_comments=False,
        include_tables=False,
    ) or ""

    title: str | None = None
    published_at: str | None = None
    try:
        metadata = trafilatura.extract_metadata(html, default_url=url)
        if metadata is not None:
            title = metadata.title or None
            published_at = metadata.date or None
    except Exception:
        # trafilatura's metadata extraction is best-effort; never fail the fetch over it.
        pass

    if not title:
        title = _html_title_fallback(html)

    return ExtractedContent(
        title=title,
        body=body,
        published_at=published_at,
        language=None,  # filled in by the pipeline via langdetect (FR-12)
    )


def _html_title_fallback(html: bytes) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("title")
    if tag is None:
        return None
    text = tag.get_text(strip=True)
    return text or None


# --- RSS / Atom ---------------------------------------------------------------

def extract_feed(body: bytes) -> list[FeedItem]:
    """Parse a feed and return one FeedItem per usable entry.

    Entries without a `link` or `id` are skipped — they cannot be deduplicated
    or re-fetched.
    """
    parsed = feedparser.parse(body)
    feed_language = (parsed.feed.get("language") or "").strip() or None
    items: list[FeedItem] = []
    for entry in parsed.entries:
        item = _entry_to_item(entry, feed_language=feed_language)
        if item is not None:
            items.append(item)
    return items


def extract_feed_entry(entry: Any, *, feed_language: str | None = None) -> FeedItem | None:
    """Extract a single feedparser entry. Returns None if the entry has no URL."""
    return _entry_to_item(entry, feed_language=feed_language)


def _entry_to_item(entry: Any, *, feed_language: str | None) -> FeedItem | None:
    url = entry.get("link") or entry.get("id")
    if not url:
        return None

    title = entry.get("title") or None

    body_text = ""
    content_list = entry.get("content")
    if content_list:
        body_text = content_list[0].get("value", "") or ""
    if not body_text:
        body_text = entry.get("summary", "") or ""
    body_text = _strip_html(body_text)

    published_at = entry.get("published") or entry.get("updated") or None
    language = entry.get("language") or feed_language
    if language:
        language = language[:10]

    return FeedItem(
        url=url,
        content=ExtractedContent(
            title=title,
            body=body_text,
            published_at=published_at,
            language=language,
        ),
    )


def _strip_html(s: str) -> str:
    if not s:
        return ""
    return BeautifulSoup(s, "lxml").get_text(" ", strip=True)
