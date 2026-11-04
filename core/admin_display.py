"""Small display helpers shared by the admin pages of several apps."""
from __future__ import annotations

from django.utils.formats import date_format
from django.utils.html import format_html


def pill(kind: str, label: str):
    """A coloured status label. `kind` is one of ok, warn, bad or off."""
    return format_html('<span class="status-pill {}">{}</span>', kind, label)


def titled(title: str, details: str):
    """A list cell with a main line and a smaller line under it."""
    return format_html(
        '<span class="article-title">{}</span><span class="article-from">{}</span>',
        title, details,
    )


def with_note(value, note: str):
    """A plain value with a smaller line under it."""
    return format_html('{}<span class="article-from">{}</span>', value, note)


def short_date(value) -> str | None:
    return date_format(value, "M j, Y") if value else None


def short_datetime(value) -> str | None:
    return date_format(value, "M j, Y H:i") if value else None
