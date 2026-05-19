"""Layer 1 dedup: URL fingerprinting via canonicalization + sha256.

The fingerprint is the sha256 hex digest of the canonical URL. Two URLs are
considered the same iff their canonical forms agree byte-for-byte. The
canonicalizer:
    - lowercases scheme and host
    - strips default ports (80 for http, 443 for https)
    - strips leading 'www.' from the host
    - drops fragments
    - drops tracking query params (utm_*, fbclid, gclid, ...)
    - sorts remaining query params
    - removes a trailing slash from non-root paths

Uniqueness is enforced at the dedup-pipeline level, not by a DB constraint, to
avoid race conditions on concurrent crawl workers.
"""
from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAM_NAMES = frozenset({
    "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid",
    "ref", "ref_src", "_hsenc", "_hsmi", "yclid", "igshid",
})


def canonicalize(url: str) -> str:
    """Return the canonical form of `url`."""
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    port = parts.port
    if port is not None and (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        port = None

    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"

    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query_pairs = [
        (k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking_param(k)
    ]
    query_pairs.sort()
    query = urlencode(query_pairs, doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def fingerprint(url: str) -> str:
    """Return a sha256 hex digest of the canonical URL (64 chars)."""
    return hashlib.sha256(canonicalize(url).encode("utf-8")).hexdigest()


def find_duplicate(url: str) -> int | None:
    """Return the id of the canonical RawArticle whose URL matches, or None."""
    from raw_content.models import RawArticle

    return (
        RawArticle.objects
        .filter(url_fingerprint=fingerprint(url), is_duplicate=False)
        .values_list("id", flat=True)
        .first()
    )


def _is_tracking_param(name: str) -> bool:
    if name in _TRACKING_PARAM_NAMES:
        return True
    return any(name.startswith(prefix) for prefix in _TRACKING_PARAM_PREFIXES)
