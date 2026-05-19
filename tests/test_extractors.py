"""Extractor tests for HTML (trafilatura) and RSS/Atom (feedparser)."""
from __future__ import annotations

import feedparser

from crawler.extractors.base import (
    ExtractedContent,
    FeedItem,
    extract_feed,
    extract_feed_entry,
    extract_html,
)


HTML_SAMPLE = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <title>Dengue outbreak reported in District X</title>
  <meta property="article:published_time" content="2026-05-19T10:00:00Z">
</head>
<body>
  <header>Site nav | Some menu | Login</header>
  <article>
    <h1>Dengue outbreak reported in District X</h1>
    <p>The Ministry of Health reported 50 suspected cases of dengue in District X on Monday,
    prompting an immediate response from local authorities. Health officials said they were
    deploying field teams to identify additional cases and conduct vector-control activities.</p>
    <p>Local hospitals have been put on alert and additional medical supplies were dispatched
    to the affected area. The community has been urged to take preventive measures including
    eliminating standing water and using insect repellent.</p>
    <p>This is the second outbreak reported in the region this year, raising concerns among
    public-health officials about the spread of the disease in densely populated areas.</p>
  </article>
  <footer>Copyright nav links</footer>
</body>
</html>
"""


RSS_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>Regional Health News</title>
  <language>en-us</language>
  <item>
    <title>Cholera cases rise in coastal region</title>
    <link>https://example.com/cholera-rise</link>
    <description><![CDATA[Health officials report a <b>surge</b> in cholera cases.]]></description>
    <pubDate>Mon, 19 May 2026 12:00:00 GMT</pubDate>
  </item>
  <item>
    <title>H5N1 detected in poultry farm</title>
    <link>https://example.com/h5n1</link>
    <description>WHO confirms avian flu detection.</description>
  </item>
  <item>
    <title>Entry with no link should be skipped</title>
    <description>This should not appear.</description>
  </item>
</channel>
</rss>
"""


# --- HTML ---------------------------------------------------------------------

def test_extract_html_returns_title_and_body():
    result = extract_html(HTML_SAMPLE, url="https://example.com/dengue")

    assert isinstance(result, ExtractedContent)
    assert result.title is not None
    assert "Dengue outbreak" in result.title
    assert "Ministry of Health" in result.body
    assert "50 suspected cases" in result.body


def test_extract_html_strips_nav_and_footer_boilerplate():
    result = extract_html(HTML_SAMPLE, url="https://example.com/dengue")

    # trafilatura should drop site nav / footer from the extracted body.
    assert "Site nav" not in result.body
    assert "Copyright nav links" not in result.body


def test_extract_html_language_is_none_until_pipeline_detects_it():
    """Language detection is the pipeline's job (FR-12), not the extractor's."""
    result = extract_html(HTML_SAMPLE, url="https://example.com/dengue")
    assert result.language is None


def test_extract_html_falls_back_to_title_tag_when_metadata_empty():
    minimal = b"<html><head><title>Fallback Title</title></head><body><p>x</p></body></html>"
    result = extract_html(minimal, url="https://example.com/x")
    assert result.title == "Fallback Title"


def test_extract_html_handles_unparseable_input_gracefully():
    result = extract_html(b"not really html", url="https://example.com/x")
    assert isinstance(result, ExtractedContent)
    # Body may be empty; we just require the call to not raise.
    assert result.body == "" or isinstance(result.body, str)


# --- RSS / Atom ---------------------------------------------------------------

def test_extract_feed_returns_one_item_per_usable_entry():
    items = extract_feed(RSS_SAMPLE)
    # Third entry has no link and must be skipped.
    assert len(items) == 2
    assert all(isinstance(i, FeedItem) for i in items)


def test_extract_feed_parses_link_title_and_body():
    items = extract_feed(RSS_SAMPLE)
    by_url = {i.url: i for i in items}

    cholera = by_url["https://example.com/cholera-rise"]
    assert cholera.content.title == "Cholera cases rise in coastal region"
    # HTML inside description must be stripped to plain text.
    assert "<b>" not in cholera.content.body
    assert "surge" in cholera.content.body


def test_extract_feed_inherits_channel_language_when_entry_lacks_one():
    items = extract_feed(RSS_SAMPLE)
    # Both entries inherit en-us from <channel><language>.
    assert all(i.content.language == "en-us" for i in items)


def test_extract_feed_captures_published_date_when_present():
    items = extract_feed(RSS_SAMPLE)
    by_url = {i.url: i for i in items}
    assert by_url["https://example.com/cholera-rise"].content.published_at is not None
    assert by_url["https://example.com/h5n1"].content.published_at is None


def test_extract_feed_entry_returns_none_for_linkless_entry():
    parsed = feedparser.parse(RSS_SAMPLE)
    linkless = [e for e in parsed.entries if not e.get("link") and not e.get("id")][0]
    assert extract_feed_entry(linkless) is None
