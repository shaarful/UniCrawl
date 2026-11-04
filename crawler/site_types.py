"""The kinds of site articles come from, used to report what could and could not be crawled."""
from __future__ import annotations

# Original links that cannot be fetched anonymously: GPHIN is subscription-only,
# and social posts sit behind anti-bot walls (no circumvention, SRS §2.5).
GPHIN_SOURCE_IDS = frozenset({"gphin"})
_SOCIAL_MEDIA_PREFIX = "socialmedia"

GPHIN = "GPHIN"
SOCIAL_MEDIA = "Social media"
UNKNOWN = "Unknown"

# Anything else is shown as EIOS names it, so a new type gets its own row by itself.
_LABELS = {
    "webnews": "Web news",
    "wire": "Wire",
    "html": "Website",
    "rss": "RSS feed",
    "eios": "No type from EIOS",
}


def is_social_media(source_type: str | None) -> bool:
    return (source_type or "").lower().startswith(_SOCIAL_MEDIA_PREFIX)


def site_type(source_type: str | None, is_gphin: bool = False) -> str:
    """The name of a kind of site. GPHIN is listed under the type "Medical", so it
    can only be told apart by its EIOS source id."""
    if is_gphin:
        return GPHIN
    if is_social_media(source_type):
        return SOCIAL_MEDIA
    source_type = (source_type or "").strip()
    return _LABELS.get(source_type.lower(), source_type) or UNKNOWN
