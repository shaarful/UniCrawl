"""EIOS fetcher and pipeline tests — network-free via httpx.MockTransport."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest

from crawler.fetchers.eios import EIOSFetcher, board_url, parse_board_url
from crawler.pipeline import run_for_source
from raw_content.models import RawArticle
from sources.models import Source

TOKEN_URL = "https://login.example.com/token"
BOARD_URL = board_url("https://eios.example.org/portal/api", "1.0", "42")
T0 = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


def _fetcher(handler, **kwargs) -> EIOSFetcher:
    return EIOSFetcher(
        token_url=TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        scope="api://eios/.default",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _token_response(token="tok-1"):
    return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3599, "access_token": token})


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def test_board_url_round_trips():
    assert BOARD_URL == "https://eios.example.org/portal/api/api/v1.0/Items/matching-board/42"
    assert parse_board_url(BOARD_URL) == ("https://eios.example.org/portal/api/api/v1.0", "42")


def test_parse_board_url_rejects_other_urls():
    with pytest.raises(ValueError):
        parse_board_url("https://example.com/feed.xml")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_token_is_requested_once_and_sent_as_bearer():
    calls = {"token": 0}
    auth_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            calls["token"] += 1
            body = request.content.decode()
            assert "grant_type=client_credentials" in body
            assert "client_id=cid" in body
            return _token_response()
        auth_headers.append(request.headers["authorization"])
        return httpx.Response(200, json={"result": [], "count": 0})

    fetcher = _fetcher(handler)
    fetcher.fetch(BOARD_URL)
    fetcher.fetch(BOARD_URL)

    assert calls["token"] == 1
    assert auth_headers == ["Bearer tok-1", "Bearer tok-1"]


def test_token_is_refreshed_once_on_401():
    tokens = iter(["stale", "fresh"])

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return _token_response(next(tokens))
        if request.headers["authorization"] == "Bearer stale":
            return httpx.Response(401)
        return httpx.Response(200, json={"result": [], "count": 0})

    assert _fetcher(handler).fetch(BOARD_URL).status_code == 200


def test_missing_credentials_raise():
    fetcher = EIOSFetcher(token_url="", client_id="", client_secret="", scope="",
                          transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(RuntimeError, match="EIOS credentials missing"):
        fetcher.fetch(BOARD_URL)


# ---------------------------------------------------------------------------
# Board paging
# ---------------------------------------------------------------------------

def _board_handler(articles_by_window: dict[str, int], requests: list[httpx.Request]):
    """Serve `n` synthetic articles for each timeSince value."""
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return _token_response()
        requests.append(request)
        since = request.url.params["timeSince"]
        n = articles_by_window.get(since, 0)
        start = int(request.url.params["start"])
        limit = int(request.url.params["limit"])
        result = [{"id": f"{since}-{i}"} for i in range(start, min(start + limit, n))]
        return httpx.Response(200, json={"result": result, "count": n})
    return handler


def test_iter_board_items_pages_through_each_window():
    requests: list[httpx.Request] = []
    handler = _board_handler({"2026-10-25T00:00:00Z": 250, "2026-10-25T01:00:00Z": 3}, requests)

    items = list(_fetcher(handler, page_size=100, window_minutes=60).iter_board_items(
        BOARD_URL, since=T0, until=T0 + timedelta(hours=2)))

    assert len(items) == 253
    assert len({i["id"] for i in items}) == 253
    first_window = [r for r in requests if r.url.params["timeSince"] == "2026-10-25T00:00:00Z"]
    assert [r.url.params["start"] for r in first_window] == ["0", "100", "200"]
    assert first_window[0].url.params["timeUntil"] == "2026-10-25T01:00:00Z"


def test_window_over_result_limit_is_split():
    requests: list[httpx.Request] = []
    # The full hour reports 10,000+; each half-hour reports 2.
    handler = _board_handler({
        "2026-10-25T00:00:00Z": 2,
        "2026-10-25T00:30:00Z": 2,
    }, requests)

    def splitting_handler(request: httpx.Request) -> httpx.Response:
        if (str(request.url) != TOKEN_URL and request.url.params["timeSince"] == "2026-10-25T00:00:00Z"
                and request.url.params["timeUntil"] == "2026-10-25T01:00:00Z"):
            return httpx.Response(200, json={"result": [{"id": "x"}], "count": 12_000})
        return handler(request)

    items = list(_fetcher(splitting_handler, window_minutes=60).iter_board_items(
        BOARD_URL, since=T0, until=T0 + timedelta(hours=1)))

    assert len(items) == 4
    assert "x" not in {i["id"] for i in items}


def test_board_http_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return _token_response()
        return httpx.Response(403, json={"Message": "not authorized"})

    with pytest.raises(httpx.HTTPStatusError):
        list(_fetcher(handler).iter_board_items(BOARD_URL, since=T0, until=T0 + timedelta(hours=1)))


def test_get_item_returns_none_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return _token_response()
        assert request.url.path == "/portal/api/api/v1.0/Items/abc"
        return httpx.Response(404)

    assert _fetcher(handler).get_item(BOARD_URL, "abc") is None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def _item(id_, title, *, link=None, description="", source_id="news", source_type="webnews", **extra):
    return {
        "id": id_,
        "title": title,
        "link": link or f"https://news.example.com/{id_}",
        "description": description,
        "languageIso": "en",
        "pubDate": "2026-10-25T06:00:00Z",
        "source": {"id": source_id, "type": source_type, "name": source_id},
        "countries": [{"key": "KE", "label": "Kenya"}],
        **extra,
    }


def _eios_source(**kwargs):
    return Source.objects.create(
        name="EIOS", url=BOARD_URL, content_type=Source.ContentType.EIOS,
        keyword_filter_overrides=["ebola"], **kwargs,
    )


def _fake_fetcher(items, detail=None):
    fetcher = MagicMock()
    fetcher.iter_board_items.return_value = iter(items)
    fetcher.get_item.return_value = detail
    return fetcher


def _run(source, fetcher, original_body="Full article text about the Ebola outbreak. " * 20):
    with patch("crawler.pipeline._fetcher_for", return_value=fetcher), \
         patch("crawler.publisher.publish_matched"), \
         patch("crawler.pipeline._fetch_original_body", return_value=original_body) as fetch_link:
        run_for_source(source.id)
    return fetch_link


@pytest.mark.django_db
def test_eios_pipeline_stores_every_item_and_enriches_only_matches():
    source = _eios_source()
    fetcher = _fake_fetcher([
        _item("a", "Ebola claims second life in Kenya", description="Kenya Ebola snippet."),
        _item("b", "Local football results", description="Sports snippet."),
    ], detail={"id": "a"})

    fetch_link = _run(source, fetcher)

    assert RawArticle.objects.filter(source=source).count() == 2
    matched = RawArticle.objects.get(url="https://news.example.com/a")
    other = RawArticle.objects.get(url="https://news.example.com/b")
    assert matched.is_keyword_matched
    assert matched.body.startswith("Full article text")
    assert matched.metadata["body_source"] == "original_link"
    assert matched.metadata["eios"]["id"] == "a"
    assert matched.metadata["eios"]["countries"] == [{"key": "KE", "label": "Kenya"}]
    assert not other.is_keyword_matched
    assert other.body == "Sports snippet."
    assert other.metadata["body_source"] == "eios_snippet"
    fetch_link.assert_called_once_with("https://news.example.com/a")
    fetcher.close.assert_called_once()


@pytest.mark.django_db
def test_eios_pipeline_prefers_eios_fulltext():
    source = _eios_source()
    fetcher = _fake_fetcher([_item("a", "Ebola update")], detail={"id": "a", "fullText": "EIOS full Ebola text."})

    fetch_link = _run(source, fetcher)

    article = RawArticle.objects.get()
    assert article.body == "EIOS full Ebola text."
    assert article.metadata["body_source"] == "eios_fulltext"
    fetch_link.assert_not_called()


@pytest.mark.django_db
def test_eios_pipeline_stops_asking_for_fulltext_after_repeated_misses():
    source = _eios_source()
    items = [_item(str(i), f"Ebola report {i}") for i in range(5)]
    fetcher = _fake_fetcher(items, detail={"id": "x"})

    _run(source, fetcher)

    assert fetcher.get_item.call_count == 3


@pytest.mark.django_db
def test_eios_pipeline_does_not_fetch_social_or_gphin_links():
    source = _eios_source()
    fetcher = _fake_fetcher([
        _item("fb", "Ebola rumour", description="Ebola post.", source_type="socialmedia-facebook"),
        _item("gp", "Ebola alert", description="Ebola GPHIN.", source_id="gphin", source_type="Medical"),
    ])

    fetch_link = _run(source, fetcher)

    fetch_link.assert_not_called()
    assert {a.metadata["body_source"] for a in RawArticle.objects.all()} == {"eios_snippet"}
    assert RawArticle.objects.filter(is_keyword_matched=True).count() == 2


@pytest.mark.django_db
def test_eios_pipeline_keeps_snippet_when_link_body_is_shorter():
    source = _eios_source()
    fetcher = _fake_fetcher([_item("a", "Ebola", description="A long Ebola snippet " * 10)])

    _run(source, fetcher, original_body="Subscribe")

    article = RawArticle.objects.get()
    assert article.body.startswith("A long Ebola snippet")
    assert article.metadata["body_source"] == "eios_snippet"


@pytest.mark.django_db
def test_eios_pipeline_matches_on_english_translation():
    source = _eios_source()
    fetcher = _fake_fetcher([_item(
        "hi", "डेंगू", description="हिंदी पाठ", translatedTitle="Ebola cases rise",
    )])

    _run(source, fetcher)

    assert RawArticle.objects.get().is_keyword_matched


@pytest.mark.django_db
def test_eios_pipeline_skips_items_already_stored_for_source():
    source = _eios_source()
    _run(source, _fake_fetcher([_item("a", "Ebola")]))
    fetch_link = _run(source, _fake_fetcher([_item("a", "Ebola"), _item("b", "Ebola again")]))

    assert RawArticle.objects.filter(url="https://news.example.com/a").count() == 1
    assert RawArticle.objects.filter(url="https://news.example.com/b").exists()
    fetch_link.assert_called_once_with("https://news.example.com/b")


@pytest.mark.django_db
def test_eios_window_uses_backfill_then_last_crawl(settings):
    settings.EIOS_BACKFILL_HOURS = 6
    source = _eios_source()
    fetcher = _fake_fetcher([])
    _run(source, fetcher)
    first = fetcher.iter_board_items.call_args.kwargs
    assert first["until"] - first["since"] == timedelta(hours=6)

    source.refresh_from_db()
    fetcher = _fake_fetcher([])
    _run(source, fetcher)
    second = fetcher.iter_board_items.call_args.kwargs
    assert second["since"] == source.last_crawled_at - timedelta(minutes=15)
