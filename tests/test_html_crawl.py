"""HTML sources: rendering JavaScript shells and following links from list pages."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

import httpx
import pytest

from crawler import pipeline
from crawler.fetchers.base import FetchResult
from crawler.fetchers.browser import (
    BrowserRenderer,
    BrowserUnavailable,
    PageLoader,
    RenderFailed,
    needs_rendering,
)
from crawler.links import RobotsRules, article_links
from crawler.progress import NullProgress
from raw_content.models import RawArticle
from sources.models import Source

BASE = "https://example.org"
SHELL = (b'<html><head><title>News</title><script src="/app.js"></script></head>'
         b'<body><div id="app"></div></body></html>')
TEXT = " ".join(f"Sentence number {n} of this long enough article about health." for n in range(40))


def _article_page(title="Story"):
    return (f"<html><head><title>{title}</title></head><body><article><h1>{title}</h1>"
            f"<p>{TEXT} {title}.</p></article></body></html>").encode()


def _list_page(*paths, nav=("/about-us", "/contact")):
    links = "".join(f'<li><a href="{path}">Title of {path}</a></li>' for path in paths)
    menu = "".join(f'<a href="{path}">menu</a>' for path in nav)
    return f"<html><body><nav>{menu}</nav><ul>{links}</ul></body></html>".encode()


def _result(url, body=b"", status=200, content_type="text/html"):
    return FetchResult(url=url, status_code=status, body=body,
                       headers={"content-type": content_type}, final_url=url)


def _site(pages):
    """A fetch function serving {url: body or FetchResult}; anything else is a 404."""
    def fetch(url):
        page = pages.get(url)
        if page is None:
            return _result(url, b"", status=404)
        return page if isinstance(page, FetchResult) else _result(url, page)
    return fetch


def _source(depth=2, url=f"{BASE}/news", **kwargs):
    return Source.objects.create(
        name="Site", url=url, content_type=Source.ContentType.HTML, crawl_depth=depth, **kwargs,
    )


class RecordingProgress(NullProgress):
    def __init__(self):
        self.events = []
        self.skipped = 0

    def event(self, level, message):
        self.events.append((level, message))

    def skip(self, count=1):
        self.skipped += count

    def messages(self, level):
        return [message for found, message in self.events if found == level]


@pytest.fixture(autouse=True)
def _no_delay(settings):
    settings.CRAWLER_LINK_DELAY_SECONDS = 0
    settings.CRAWLER_MAX_PAGES_PER_RUN = 50


def _crawl(source, pages, progress=None):
    with patch("crawler.fetchers.html.HTMLFetcher.fetch", side_effect=_site(pages)) as fetch, \
         patch("crawler.publisher.publish_matched"):
        pipeline.run_for_source(source.pk, progress=progress)
    return fetch


# --- Recognising an empty shell ---------------------------------------------------

def test_a_javascript_shell_needs_rendering():
    assert needs_rendering(SHELL)


def test_a_normal_article_does_not():
    assert not needs_rendering(_article_page())


def test_a_short_page_without_scripts_does_not():
    assert not needs_rendering(b"<html><body><p>Short notice.</p></body></html>")


def test_a_page_with_scripts_and_real_text_does_not():
    assert not needs_rendering(_article_page().replace(b"</head>", b"<script></script></head>"))


def test_a_list_page_with_little_text_but_many_links_does_not():
    links = b'<a href="/a">a</a>' * 6
    page = b"<html><head><script></script></head><body>" + links + b"</body></html>"
    assert not needs_rendering(page)


# --- PageLoader -------------------------------------------------------------------

def _loader(result, **kwargs):
    fetcher = MagicMock()
    fetcher.fetch.return_value = result
    fetcher.user_agent = "UA"
    return PageLoader(fetcher, **kwargs), fetcher


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_normal_pages_never_start_the_browser(renderer):
    loader, _ = _loader(_result(f"{BASE}/a", _article_page()))

    assert loader.load(f"{BASE}/a").body == _article_page()
    assert not renderer.called


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_shell_pages_are_rendered(renderer):
    rendered = _result(f"{BASE}/a", _article_page())
    renderer.return_value.render.return_value = rendered
    loader, _ = _loader(_result(f"{BASE}/a", SHELL))

    assert loader.load(f"{BASE}/a") is rendered
    renderer.assert_called_once_with("UA")


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_error_responses_are_not_rendered(renderer):
    loader, _ = _loader(_result(f"{BASE}/a", SHELL, status=403))

    assert loader.load(f"{BASE}/a").status_code == 403
    assert not renderer.called


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_after_one_shell_the_rest_of_the_site_is_rendered(renderer):
    renderer.return_value.render.return_value = _result(f"{BASE}/x", _article_page())
    loader, fetcher = _loader(_result(f"{BASE}/a", SHELL))
    loader.load(f"{BASE}/a")

    fetcher.fetch.return_value = _result(f"{BASE}/b", _article_page())
    loader.load(f"{BASE}/b")

    assert renderer.return_value.render.call_count == 2


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_browser_can_be_disabled(renderer, settings):
    settings.CRAWLER_BROWSER_ENABLED = False
    loader, _ = _loader(_result(f"{BASE}/a", SHELL))

    assert loader.load(f"{BASE}/a").body == SHELL
    assert not renderer.called


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_browser_that_cannot_start_warns_once_and_uses_the_plain_page(renderer):
    renderer.return_value.render.side_effect = BrowserUnavailable("not installed")
    warnings = []
    loader, _ = _loader(_result(f"{BASE}/a", SHELL), on_warning=warnings.append)

    assert loader.load(f"{BASE}/a").body == SHELL
    assert loader.load(f"{BASE}/b").body == SHELL
    assert len(warnings) == 1
    assert "not installed" in warnings[0]
    assert renderer.return_value.render.call_count == 1


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_a_page_that_fails_to_render_does_not_stop_the_browser(renderer):
    good = _result(f"{BASE}/b", _article_page())
    renderer.return_value.render.side_effect = [RenderFailed("timeout"), good]
    warnings = []
    loader, _ = _loader(_result(f"{BASE}/a", SHELL), on_warning=warnings.append)

    assert loader.load(f"{BASE}/a").body == SHELL
    assert loader.load(f"{BASE}/b") is good
    assert len(warnings) == 1


@patch("crawler.fetchers.browser.BrowserRenderer")
def test_close_stops_the_browser(renderer):
    renderer.return_value.render.return_value = _result(f"{BASE}/a", _article_page())
    loader, _ = _loader(_result(f"{BASE}/a", SHELL))
    loader.load(f"{BASE}/a")

    loader.close()

    renderer.return_value.close.assert_called_once()


def test_browser_identifies_itself_as_the_crawler():
    renderer = BrowserRenderer("RZDO/test")
    playwright = MagicMock()
    with patch("playwright.sync_api.sync_playwright") as sync_playwright:
        sync_playwright.return_value.start.return_value = playwright
        renderer._start()

    browser = playwright.chromium.launch.return_value
    browser.new_context.assert_called_once_with(user_agent="RZDO/test")


def test_browser_that_is_not_installed_says_how_to_install_it():
    from playwright.sync_api import Error

    renderer = BrowserRenderer("UA")
    with patch("playwright.sync_api.sync_playwright") as sync_playwright:
        sync_playwright.return_value.start.side_effect = Error("Executable doesn't exist")
        with pytest.raises(BrowserUnavailable, match="playwright install chromium"):
            renderer._start()


@pytest.mark.parametrize("resource, blocked", [("image", True), ("font", True), ("script", False)])
def test_only_unneeded_resources_are_blocked(resource, blocked):
    route = MagicMock()
    route.request.resource_type = resource

    BrowserRenderer._filter_request(route)

    assert route.abort.called is blocked
    assert route.continue_.called is not blocked


@pytest.fixture
def javascript_site():
    """A local page whose text only exists once its script has run."""
    page = (b'<html><head><title>Shell</title></head><body><div id="app"></div><script>'
            b'document.getElementById("app").textContent = "Built by JavaScript";'
            b"</script></body></html>")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()


@pytest.mark.django_db
def test_real_browser_renders_and_leaves_the_database_usable(javascript_site):
    """Not mocked: Playwright on the crawl's own thread makes Django refuse database calls."""
    renderer = BrowserRenderer("RZDO/test")
    try:
        try:
            result = renderer.render(javascript_site)
        except BrowserUnavailable:
            pytest.skip("Chromium is not installed")

        assert b"Built by JavaScript" in result.body
        assert result.status_code == 200
        assert Source.objects.count() == 0  # raises SynchronousOnlyOperation if on this thread
    finally:
        renderer.close()


# --- Choosing links ---------------------------------------------------------------

def test_links_under_the_list_page_are_chosen_over_navigation():
    nav = ("/about-us", "/clinical-care-and-research-services")
    html = _list_page("/news/story-one", "/news/story-two", nav=nav)

    urls = [link.url for link in article_links(html, f"{BASE}/news")]

    assert urls == [f"{BASE}/news/story-one", f"{BASE}/news/story-two"]


def test_menu_links_are_not_mistaken_for_articles():
    html = (b'<html><body><header><a href="/resources/facts-in-pictures-today">a</a></header>'
            b'<nav><a href="/mega-menu/health-topics-resources-list">b</a></nav>'
            b'<nav><div><nav><a href="/nested/menu-link-here-now">c</a></nav></div></nav>'
            b'<footer><a href="/site/legal-notice-and-terms">d</a></footer></body></html>')

    assert article_links(html, f"{BASE}/latest") == []


def test_an_article_cards_own_header_link_is_kept():
    html = (b'<html><body><main><article><header><a href="/news/one">One</a></header>'
            b"</article></main></body></html>")

    assert [link.url for link in article_links(html, f"{BASE}/news")] == [f"{BASE}/news/one"]


def test_link_text_is_kept_for_use_as_a_title():
    assert article_links(_list_page("/news/a"), f"{BASE}/news")[0].text == "Title of /news/a"


def test_links_to_other_sites_are_ignored():
    html = _list_page("https://other.example.com/news/story", "/news/mine")

    assert [link.url for link in article_links(html, f"{BASE}/news")] == [f"{BASE}/news/mine"]


def test_www_counts_as_the_same_site():
    html = _list_page("https://www.example.org/news/story")

    assert len(article_links(html, f"{BASE}/news")) == 1


@pytest.mark.parametrize(
    "path",
    ["/news/report.pdf", "/news/photo.JPG", "/news/tag/health", "/news/category/x", "/news",
     "/news/page/2", "mailto:a@example.org", "javascript:void(0)", "tel:123"],
)
def test_files_lists_and_non_web_links_are_ignored(path):
    assert article_links(_list_page(path), f"{BASE}/news") == []


def test_fragments_do_not_create_repeat_links():
    html = _list_page("/news/story", "/news/story#comments", "/news/story#top")

    assert [link.url for link in article_links(html, f"{BASE}/news")] == [f"{BASE}/news/story"]


def test_page_order_is_kept():
    html = _list_page("/news/z-newest", "/news/a-older")

    assert [link.url.rsplit("/", 1)[-1] for link in article_links(html, f"{BASE}/news")] == [
        "z-newest", "a-older",
    ]


def test_relative_links_resolve_against_the_page():
    html = _list_page("story")

    assert article_links(html, f"{BASE}/news/")[0].url == f"{BASE}/news/story"


def test_without_links_under_the_page_article_like_links_are_chosen():
    html = _list_page(
        "/2026/10/dengue-cases-rise-in-dhaka", "/story/1234567", "/health", "/about-us",
        "/our-team",
    )

    urls = [link.url.replace(BASE, "") for link in article_links(html, f"{BASE}/latest")]

    assert urls == ["/2026/10/dengue-cases-rise-in-dhaka", "/story/1234567"]


def test_a_home_page_list_uses_article_like_links():
    html = _list_page("/dengue-cases-rise-in-dhaka", "/health")

    assert [link.url.replace(BASE, "") for link in article_links(html, f"{BASE}/")] == [
        "/dengue-cases-rise-in-dhaka",
    ]


# --- robots.txt -------------------------------------------------------------------

def _robots(fetch):
    fetcher = MagicMock()
    fetcher.user_agent = "RZDO/0.1"
    fetcher.fetch.side_effect = fetch
    return RobotsRules(fetcher), fetcher


def test_robots_disallow_is_respected():
    body = b"User-agent: *\nDisallow: /private/\n"
    rules, _ = _robots(lambda url: _result(url, body))

    assert rules.allowed(f"{BASE}/news/a")
    assert not rules.allowed(f"{BASE}/private/a")


def test_robots_is_read_once_per_site():
    rules, fetcher = _robots(lambda url: _result(url, b""))

    rules.allowed(f"{BASE}/a")
    rules.allowed(f"{BASE}/b")

    fetcher.fetch.assert_called_once_with(f"{BASE}/robots.txt")


@pytest.mark.parametrize(
    "fetch",
    [lambda url: _result(url, b"", status=404), MagicMock(side_effect=httpx.ConnectError("down"))],
    ids=["missing", "unreachable"],
)
def test_unreadable_robots_allows_everything(fetch):
    rules, _ = _robots(fetch)

    assert rules.allowed(f"{BASE}/a")


# --- Crawling: depth 2 ------------------------------------------------------------

@pytest.mark.django_db
def test_list_page_stores_its_articles_and_not_itself():
    source = _source()
    _crawl(source, {
        f"{BASE}/news": _list_page("/news/one", "/news/two"),
        f"{BASE}/news/one": _article_page("One"),
        f"{BASE}/news/two": _article_page("Two"),
    })

    stored = RawArticle.objects.filter(source=source)
    assert {article.url for article in stored} == {f"{BASE}/news/one", f"{BASE}/news/two"}
    assert all(article.body and article.source_type == "html" for article in stored)


@pytest.mark.django_db
def test_next_crawl_requests_only_new_articles():
    source = _source()
    pages = {
        f"{BASE}/news": _list_page("/news/one"),
        f"{BASE}/news/one": _article_page("One"),
    }
    _crawl(source, pages)
    pages[f"{BASE}/news"] = _list_page("/news/two", "/news/one")
    pages[f"{BASE}/news/two"] = _article_page("Two")
    progress = RecordingProgress()

    fetch = _crawl(source, pages, progress)

    requested = [call.args[0] for call in fetch.call_args_list]
    assert f"{BASE}/news/two" in requested
    assert f"{BASE}/news/one" not in requested
    assert RawArticle.objects.filter(source=source).count() == 2
    assert progress.skipped == 1


@pytest.mark.django_db
def test_page_cap_stops_the_run_and_the_next_crawl_continues(settings):
    settings.CRAWLER_MAX_PAGES_PER_RUN = 2
    source = _source()
    pages = {f"{BASE}/news": _list_page("/news/a", "/news/b", "/news/c")}
    pages.update({f"{BASE}/news/{n}": _article_page(n) for n in "abc"})
    progress = RecordingProgress()

    _crawl(source, pages, progress)
    assert RawArticle.objects.filter(source=source).count() == 2
    assert any("Stopped at 2 pages" in message for message in progress.messages("info"))

    _crawl(source, pages)
    assert RawArticle.objects.filter(source=source).count() == 3


@pytest.mark.django_db
def test_robots_disallowed_links_are_not_requested():
    source = _source()
    fetch = _crawl(source, {
        f"{BASE}/robots.txt": b"User-agent: *\nDisallow: /news/private\n",
        f"{BASE}/news": _list_page("/news/private", "/news/open"),
        f"{BASE}/news/open": _article_page("Open"),
        f"{BASE}/news/private": _article_page("Private"),
    })

    assert f"{BASE}/news/private" not in [call.args[0] for call in fetch.call_args_list]
    assert [a.url for a in RawArticle.objects.filter(source=source)] == [f"{BASE}/news/open"]


@pytest.mark.django_db
def test_a_failed_article_is_reported_and_retried_next_crawl():
    source = _source()
    pages = {
        f"{BASE}/news": _list_page("/news/bad", "/news/good"),
        f"{BASE}/news/bad": _result(f"{BASE}/news/bad", b"", status=500),
        f"{BASE}/news/good": _article_page("Good"),
    }
    progress = RecordingProgress()

    _crawl(source, pages, progress)
    assert [a.url for a in RawArticle.objects.filter(source=source)] == [f"{BASE}/news/good"]
    assert any("/news/bad — HTTP 500" in message for message in progress.messages("warning"))

    pages[f"{BASE}/news/bad"] = _article_page("Fixed")
    _crawl(source, pages)
    assert RawArticle.objects.filter(source=source).count() == 2


@pytest.mark.django_db
def test_a_network_error_on_one_article_does_not_stop_the_others():
    source = _source()
    site = _site({
        f"{BASE}/news": _list_page("/news/bad", "/news/good"),
        f"{BASE}/news/good": _article_page("Good"),
    })

    def fetch(url):
        if url.endswith("/bad"):
            raise httpx.ConnectTimeout("slow")
        return site(url)

    with patch("crawler.fetchers.html.HTMLFetcher.fetch", side_effect=fetch):
        pipeline.run_for_source(source.pk)

    assert [a.url for a in RawArticle.objects.filter(source=source)] == [f"{BASE}/news/good"]


@pytest.mark.django_db
def test_list_page_failure_still_triggers_back_off():
    source = _source()

    with pytest.raises(RuntimeError, match="HTTP 503"):
        _crawl(source, {f"{BASE}/news": _result(f"{BASE}/news", b"", status=503)})


@pytest.mark.django_db
def test_linked_files_are_not_stored():
    source = _source()
    _crawl(source, {
        f"{BASE}/news": _list_page("/news/download"),
        f"{BASE}/news/download": _result(
            f"{BASE}/news/download", b"%PDF", content_type="application/pdf",
        ),
    })

    assert not RawArticle.objects.filter(source=source).exists()


@pytest.mark.django_db
def test_pages_without_text_are_not_stored_and_are_reported():
    source = _source()
    progress = RecordingProgress()
    _crawl(source, {
        f"{BASE}/news": _list_page("/news/empty"),
        f"{BASE}/news/empty": b"<html><body></body></html>",
    }, progress)

    assert not RawArticle.objects.filter(source=source).exists()
    assert any("1 linked pages had no article text" in m for m in progress.messages("warning"))


@pytest.mark.django_db
@patch("crawler.fetchers.browser.BrowserRenderer")
def test_list_page_without_links_even_when_rendered_warns(renderer):
    source = _source()
    renderer.return_value.render.side_effect = lambda url: _result(url, _list_page(nav=()))
    progress = RecordingProgress()

    _crawl(source, {f"{BASE}/news": _list_page(nav=())}, progress)

    assert any("No article links found" in message for message in progress.messages("warning"))


@pytest.mark.django_db
@patch("crawler.fetchers.browser.BrowserRenderer")
def test_list_hidden_behind_a_big_menu_is_found_by_rendering(renderer):
    """Many menu links stop the page looking like a shell, yet the list needs JavaScript."""
    source = _source()
    menu = tuple(f"/mega-menu/resource-page-number-{n}" for n in range(8))
    static = _list_page(nav=menu).replace(b"<html>", b"<html><head><script></script></head>")
    rendered = _list_page("/news/item/one", nav=menu)
    renderer.return_value.render.side_effect = lambda url: _result(url, rendered)
    assert not needs_rendering(static)

    _crawl(source, {
        f"{BASE}/news": static,
        f"{BASE}/news/item/one": _article_page("One"),
    })

    assert [a.url for a in RawArticle.objects.filter(source=source)] == [f"{BASE}/news/item/one"]


@pytest.mark.django_db
def test_list_page_without_links_warns_when_the_browser_is_off(settings):
    settings.CRAWLER_BROWSER_ENABLED = False
    source = _source()
    progress = RecordingProgress()

    _crawl(source, {f"{BASE}/news": _list_page(nav=())}, progress)

    assert any("No article links found" in message for message in progress.messages("warning"))


@pytest.mark.django_db
def test_link_text_is_the_title_when_the_page_has_none():
    source = _source()
    with patch("crawler.extractors.base.extract_html") as extract:
        extract.return_value = MagicMock(title=None, body="Text.", published_at=None, author=None)
        with patch("crawler.links.article_links", wraps=article_links):
            _crawl(source, {
                f"{BASE}/news": _list_page("/news/one"),
                f"{BASE}/news/one": _article_page("One"),
            })

    assert RawArticle.objects.get(source=source).title == "Title of /news/one"


@pytest.mark.django_db
def test_depth_three_follows_links_found_on_followed_pages():
    source = _source(depth=3)
    _crawl(source, {
        f"{BASE}/news": _list_page("/news/one"),
        f"{BASE}/news/one": _article_page("One").replace(
            b"</body>", b'<a href="/news/one/two">Next</a></body>'),
        f"{BASE}/news/one/two": _article_page("Two"),
    })

    assert RawArticle.objects.filter(source=source).count() == 2


@pytest.mark.django_db
def test_depth_two_does_not_follow_links_found_on_followed_pages():
    source = _source(depth=2)
    fetch = _crawl(source, {
        f"{BASE}/news": _list_page("/news/one"),
        f"{BASE}/news/one": _article_page("One").replace(
            b"</body>", b'<a href="/news/one/two">Next</a></body>'),
        f"{BASE}/news/one/two": _article_page("Two"),
    })

    assert f"{BASE}/news/one/two" not in [call.args[0] for call in fetch.call_args_list]


@pytest.mark.django_db
@patch("crawler.fetchers.browser.BrowserRenderer")
def test_a_javascript_site_is_rendered_from_list_to_articles(renderer):
    source = _source()
    rendered = {
        f"{BASE}/news": _list_page("/news/one"),
        f"{BASE}/news/one": _article_page("One"),
    }
    renderer.return_value.render.side_effect = lambda url: _result(url, rendered[url])

    _crawl(source, {f"{BASE}/news": SHELL, f"{BASE}/news/one": SHELL})

    assert RawArticle.objects.get(source=source).title == "One"
    renderer.return_value.close.assert_called_once()


@pytest.mark.django_db
@patch("crawler.fetchers.browser.BrowserRenderer")
def test_browser_is_closed_when_a_crawl_is_stopped_midway(renderer):
    from crawler.progress import CrawlCancelled

    source = _source()
    rendered = {f"{BASE}/news": _list_page("/news/one"), f"{BASE}/news/one": _article_page("One")}
    renderer.return_value.render.side_effect = lambda url: _result(url, rendered[url])

    class Stop(NullProgress):
        def article(self, outcome):
            raise CrawlCancelled

    with pytest.raises(CrawlCancelled):
        _crawl(source, {f"{BASE}/news": SHELL, f"{BASE}/news/one": SHELL}, Stop())

    renderer.return_value.close.assert_called_once()


# --- Crawling: depth 1 ------------------------------------------------------------

@pytest.mark.django_db
def test_depth_one_stores_the_page_itself_and_follows_nothing():
    source = _source(depth=1)
    fetch = _crawl(source, {f"{BASE}/news": _article_page("Only").replace(
        b"</body>", b'<a href="/news/other">Other</a></body>')})

    assert RawArticle.objects.get(source=source).url == f"{BASE}/news"
    assert [call.args[0] for call in fetch.call_args_list] == [f"{BASE}/news"]


@pytest.mark.django_db
def test_depth_one_with_no_text_stores_nothing_and_suggests_depth_two():
    source = _source(depth=1)
    progress = RecordingProgress()

    _crawl(source, {f"{BASE}/news": _list_page(nav=())}, progress)

    assert not RawArticle.objects.filter(source=source).exists()
    assert any("crawl depth to 2" in message for message in progress.messages("warning"))


# --- Refetch ----------------------------------------------------------------------

def _stored_article(source):
    return RawArticle.objects.create(
        source=source, url=f"{BASE}/news/one", http_status=200, title="One",
        fetched_at="2026-11-02T00:00:00Z", body="Old text.", url_fingerprint="f", content_hash="h",
    )


@pytest.mark.django_db
@patch("crawler.fetchers.browser.BrowserRenderer")
def test_refetch_renders_a_javascript_page(renderer):
    from crawler.refetch import refetch_article

    article = _stored_article(_source())
    renderer.return_value.render.return_value = _result(article.url, _article_page("One"))

    with patch("crawler.fetchers.html.HTMLFetcher.fetch", return_value=_result(article.url, SHELL)):
        result = refetch_article(article.pk)

    assert result.ok
    article.refresh_from_db()
    assert TEXT[:40] in article.body
    renderer.return_value.close.assert_called_once()


@pytest.mark.django_db
@patch("crawler.fetchers.browser.BrowserRenderer")
def test_refetch_does_not_start_a_browser_for_eios_articles(renderer):
    from crawler.refetch import refetch_article

    source = Source.objects.create(
        name="Board", url=f"{BASE}/board", content_type=Source.ContentType.EIOS,
    )
    article = _stored_article(source)

    with patch("crawler.fetchers.html.HTMLFetcher.fetch", return_value=_result(article.url, SHELL)):
        result = refetch_article(article.pk)

    assert not result.ok
    assert not renderer.called


@pytest.mark.django_db
@patch("crawler.fetchers.browser.BrowserRenderer")
def test_bulk_refetch_shares_one_browser(renderer):
    from crawler.refetch import refetch_articles

    source = _source()
    first = _stored_article(source)
    second = RawArticle.objects.create(
        source=source, url=f"{BASE}/news/two", http_status=200, fetched_at="2026-11-02T00:00:00Z",
        body="Old.", url_fingerprint="g", content_hash="i",
    )
    renderer.return_value.render.side_effect = lambda url: _result(url, _article_page(url[-3:]))

    with patch("crawler.fetchers.html.HTMLFetcher.fetch",
               side_effect=lambda url: _result(url, SHELL)):
        results = refetch_articles([first.pk, second.pk])

    assert all(result.ok for result in results.values())
    renderer.assert_called_once()
    renderer.return_value.close.assert_called_once()
