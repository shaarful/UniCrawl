"""Forms for the crawl dashboard."""
from __future__ import annotations

import re

from django import forms
from django.conf import settings

from crawler.fetchers.eios import board_url, parse_board_url
from sources.models import Source

_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class EIOSBoardForm(forms.Form):
    """Add a WHO-EIOS board as a source (FR-07, FR-09).

    The board is given by its ID, or by a full matching-board API URL when it
    lives on a different EIOS instance than EIOS_API_BASE_URL.
    """

    board = forms.CharField(
        label="Board ID",
        max_length=2048,
        help_text="The EIOS board ID, or a full …/Items/matching-board/{id} API URL.",
    )
    name = forms.CharField(
        max_length=255,
        required=False,
        help_text="Defaults to “WHO-EIOS board {id}”.",
    )
    interval_minutes = forms.IntegerField(
        label="Crawl every (minutes)",
        min_value=1,
        initial=lambda: max(1, settings.CRAWLER_DEFAULT_INTERVAL_SECONDS // 60),
    )
    is_active = forms.BooleanField(
        label="Schedule automatically",
        required=False,
        help_text="Leave off until a test run from the dashboard succeeds (FR-09).",
    )

    def clean_board(self) -> str:
        value = self.cleaned_data["board"].strip()
        if "://" in value:
            try:
                api_root, board_id = parse_board_url(value)
            except ValueError as exc:
                raise forms.ValidationError(str(exc)) from exc
            # Rebuilt in canonical form so the same board can't be added twice.
            return f"{api_root}/Items/matching-board/{board_id}"
        if not _BOARD_ID_RE.match(value):
            raise forms.ValidationError(
                "A board ID may only contain letters, digits, '-' and '_'.")
        if not settings.EIOS_API_BASE_URL:
            raise forms.ValidationError(
                "EIOS_API_BASE_URL is not configured — paste the full matching-board URL instead.")
        return board_url(settings.EIOS_API_BASE_URL, settings.EIOS_API_VERSION, value)

    def clean(self):
        cleaned = super().clean()
        url = cleaned.get("board")
        if not url:
            return cleaned
        existing = Source.objects.filter(url=url).first()
        if existing is not None:
            self.add_error("board", f"This board is already a source: {existing.name}.")
            return cleaned
        _, board_id = parse_board_url(url)
        name = cleaned.get("name", "").strip() or f"WHO-EIOS board {board_id}"
        if Source.objects.filter(name=name).exists():
            self.add_error("name", f"A source named “{name}” already exists.")
        cleaned["name"] = name
        return cleaned

    def save(self, *, user=None) -> Source:
        data = self.cleaned_data
        _, board_id = parse_board_url(data["board"])
        return Source.objects.create(
            name=data["name"],
            url=data["board"],
            content_type=Source.ContentType.EIOS,
            crawl_interval_seconds=data["interval_minutes"] * 60,
            is_active=data["is_active"],
            added_by=user if user is not None and user.is_authenticated else None,
            notes=f"WHO-EIOS board {board_id} via the service-to-service API (FR-07).",
        )
