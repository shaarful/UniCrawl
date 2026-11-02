"""Find article links on a list page, and check that robots.txt allows following them."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx
import structlog
from bs4 import BeautifulSoup

log = structlog.get_logger(__name__)

_FILE_EXTENSIONS = frozenset({
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".gz",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp3", ".mp4", ".avi", ".mov",
    ".css", ".js", ".json", ".xml",
})

# Path segments that lead to other lists or to site pages, not to articles.
_NON_ARTICLE_SEGMENTS = frozenset({
    "tag", "tags", "category", "categories", "author", "authors", "page", "search",
    "login", "signin", "register", "signup", "contact", "about", "about-us",
    "privacy", "terms", "feed", "rss",
})

_MIN_SLUG_WORDS = 3
_NUMBER_IN_SEGMENT = re.compile(r"\d{4,}")


@dataclass(frozen=True)
class Link:
    url: str
    text: str


def _host(netloc: str) -> str:
    return netloc.lower().removeprefix("www.")


def _looks_like_article(path: str) -> bool:
    """Articles usually end in a multi-word slug or a long number, unlike /contact."""
    segment = path.rstrip("/").rsplit("/", 1)[-1]
    words = [word for word in re.split(r"[-_]", segment.rsplit(".", 1)[0]) if word]
    return len(words) >= _MIN_SLUG_WORDS or bool(_NUMBER_IN_SEGMENT.search(segment))


def article_links(html: bytes, base_url: str) -> list[Link]:
    """Links on this page that probably lead to articles on the same site, in page order.

    Links under the page's own path are the clearest signal, so /news/some-story is
    chosen over the site's navigation when the list is at /news. If there are none,
    links are judged by how they look, which is less reliable.
    """
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    links: dict[str, Link] = {}
    soup = BeautifulSoup(html, "lxml")
    for block in soup(["nav", "header", "footer"]):
        # Site navigation, but not an article card's own <header> holding its link.
        if not block.decomposed and not block.find_parent(["article", "main"]):
            block.decompose()
    for anchor in soup.find_all("a", href=True):
        parts = urlsplit(urljoin(base_url, anchor["href"].strip()))
        path = parts.path.rstrip("/")
        extension = path[path.rfind("."):].lower() if "." in path.rsplit("/", 1)[-1] else ""
        segments = {segment.lower() for segment in path.split("/")}
        if (
            parts.scheme not in ("http", "https")
            or _host(parts.netloc) != _host(base.netloc)
            or path == base_path
            or extension in _FILE_EXTENSIONS
            or segments & _NON_ARTICLE_SEGMENTS
        ):
            continue
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        text = anchor.get_text(" ", strip=True)
        if url not in links or (text and not links[url].text):
            links[url] = Link(url=url, text=text)

    found = list(links.values())
    under_page = [link for link in found if urlsplit(link.url).path.startswith(f"{base_path}/")]
    if base_path and under_page:
        return under_page
    return [link for link in found if _looks_like_article(urlsplit(link.url).path)]


class RobotsRules:
    """robots.txt for each site, read once. An unreadable file allows everything."""

    def __init__(self, fetcher) -> None:
        self._fetcher = fetcher
        self._parsers: dict[str, RobotFileParser | None] = {}

    def _parser_for(self, origin: str) -> RobotFileParser | None:
        try:
            result = self._fetcher.fetch(f"{origin}/robots.txt")
        except httpx.HTTPError as exc:
            log.info("robots.unreadable", origin=origin, error=str(exc))
            return None
        if result.status_code >= 400:
            return None
        parser = RobotFileParser()
        parser.parse(result.body.decode("utf-8", "replace").splitlines())
        return parser

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            self._parsers[origin] = self._parser_for(origin)
        parser = self._parsers[origin]
        return parser is None or parser.can_fetch(self._fetcher.user_agent, url)
