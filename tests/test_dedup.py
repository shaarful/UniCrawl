"""Tests for the fast dedup layers (URL fingerprint + content hash)."""
from __future__ import annotations

import pytest
from django.utils import timezone

from crawler.dedup.content_hash import (
    content_hash,
    find_duplicate as find_dup_by_content,
    normalize,
)
from crawler.dedup.url_fingerprint import (
    canonicalize,
    find_duplicate as find_dup_by_url,
    fingerprint,
)


# --- URL canonicalization (no DB) ---------------------------------------------

@pytest.mark.parametrize("variant,canonical_of", [
    # scheme/host case
    ("https://Example.COM/path", "https://example.com/path"),
    # default ports
    ("https://example.com:443/path", "https://example.com/path"),
    ("http://example.com:80/path", "http://example.com/path"),
    # www. stripped
    ("https://www.example.com/x", "https://example.com/x"),
    # fragments dropped
    ("https://example.com/x#section", "https://example.com/x"),
    # tracking params stripped
    ("https://example.com/x?utm_source=foo&q=1", "https://example.com/x?q=1"),
    ("https://example.com/x?fbclid=abc&q=1", "https://example.com/x?q=1"),
    # query sorted
    ("https://example.com/x?b=2&a=1", "https://example.com/x?a=1&b=2"),
    # trailing slash on non-root path
    ("https://example.com/x/", "https://example.com/x"),
])
def test_canonicalize_normalises_equivalent_urls(variant, canonical_of):
    assert canonicalize(variant) == canonicalize(canonical_of)


def test_canonicalize_preserves_root_slash():
    # Root path slash is meaningful and must stay.
    assert canonicalize("https://example.com/").endswith("/")


def test_canonicalize_keeps_path_case_sensitivity():
    # URL paths are case-sensitive per RFC 3986 — must not be lowercased.
    assert canonicalize("https://example.com/Article") != canonicalize(
        "https://example.com/article"
    )


def test_fingerprint_is_sha256_hex_64_chars():
    fp = fingerprint("https://example.com/article")
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_is_stable_across_canonical_equivalents():
    a = fingerprint("https://Example.com/Article?utm_source=x#frag")
    b = fingerprint("https://www.example.com/Article")
    assert a == b


def test_fingerprint_differs_for_genuinely_different_urls():
    assert fingerprint("https://example.com/a") != fingerprint("https://example.com/b")


# --- Content normalization (no DB) --------------------------------------------

def test_normalize_collapses_whitespace_and_lowercases():
    assert normalize("  Hello\n\n  World  ") == "hello world"


def test_normalize_empty_string_returns_empty():
    assert normalize("") == ""


def test_content_hash_is_stable_under_whitespace_and_case():
    assert content_hash("Hello World") == content_hash("  HELLO\n\nworld  ")


def test_content_hash_differs_for_different_text():
    assert content_hash("apples") != content_hash("oranges")


def test_content_hash_is_sha256_hex_64_chars():
    h = content_hash("anything")
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


# --- DB-backed lookups --------------------------------------------------------

@pytest.fixture
def source(db):
    from sources.models import Source

    return Source.objects.create(
        name="Test source",
        url="https://example.com/feed",
        content_type=Source.ContentType.HTML,
    )


def _make_article(source, *, url: str, body: str, is_duplicate=False, duplicate_of=None):
    from raw_content.models import RawArticle

    return RawArticle.objects.create(
        source=source,
        url=url,
        http_status=200,
        fetched_at=timezone.now(),
        body=body,
        url_fingerprint=fingerprint(url),
        content_hash=content_hash(body),
        is_duplicate=is_duplicate,
        duplicate_of=duplicate_of,
        suppression_reason=(
            "url" if is_duplicate else ""
        ),
    )


@pytest.mark.django_db
def test_find_dup_by_url_returns_id_of_canonical_match(source):
    a = _make_article(source, url="https://example.com/foo", body="x")
    # Same URL with tracking + uppercase host → canonical match
    assert find_dup_by_url("https://EXAMPLE.com/foo?utm_source=x") == a.id


@pytest.mark.django_db
def test_find_dup_by_url_returns_none_when_no_match(source):
    _make_article(source, url="https://example.com/foo", body="x")
    assert find_dup_by_url("https://example.com/different") is None


@pytest.mark.django_db
def test_find_dup_by_url_skips_records_already_marked_duplicate(source):
    original = _make_article(source, url="https://example.com/foo", body="x")
    _make_article(
        source,
        url="https://example.com/foo",
        body="x",
        is_duplicate=True,
        duplicate_of=original,
    )
    # Must return the original id, not the later duplicate record.
    assert find_dup_by_url("https://example.com/foo") == original.id


@pytest.mark.django_db
def test_find_dup_by_content_finds_match_despite_whitespace_and_case(source):
    a = _make_article(source, url="https://example.com/a", body="The quick brown fox")
    assert find_dup_by_content("  the QUICK\nbrown   fox  ") == a.id


@pytest.mark.django_db
def test_find_dup_by_content_returns_none_when_no_match(source):
    _make_article(source, url="https://example.com/a", body="apples")
    assert find_dup_by_content("oranges") is None
