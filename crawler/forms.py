"""Forms for the crawl dashboard."""
from __future__ import annotations

import re
from datetime import timedelta

import httpx
from django import forms
from django.conf import settings
from django.utils import timezone

from crawler.fetchers.eios import EIOSFetcher, board_url, parse_board_url
from sources.models import Source

_BOARD_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_BOARD_CHECK_TIMEOUT_SECONDS = 15.0


class EIOSBoardForm(forms.Form):
    """Add a WHO-EIOS board as a source.

    The board is given by its ID, or by a full matching-board API URL when it
    lives on a different EIOS instance than EIOS_API_BASE_URL. EIOS is asked whether the
    board exists before it is saved, because a wrong ID otherwise only shows up as a
    failed crawl later. If EIOS cannot be reached, the board is saved with a warning.
    """

    board = forms.CharField(
        label="Board ID",
        max_length=2048,
        widget=forms.TextInput(attrs={"placeholder": "e.g. 1234567890123"}),
        help_text="The board's ID from EIOS, usually a long number, not its name. "
                  "A full …/Items/matching-board/{id} API URL also works.",
    )
    name = forms.CharField(
        label="Name",
        max_length=255,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "e.g. RZDO"}),
        help_text="Your own label for this board. Defaults to “WHO-EIOS board {id}”.",
    )
    interval_minutes = forms.IntegerField(
        label="Crawl every (minutes)",
        min_value=1,
        initial=lambda: max(1, settings.CRAWLER_DEFAULT_INTERVAL_SECONDS // 60),
    )
    is_active = forms.BooleanField(
        label="Schedule automatically",
        required=False,
        help_text="Leave off until a test run from the dashboard succeeds.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recent_articles: int | None = None  # how many a first crawl would read
        self.check_warning = ""

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
        if not self.errors:
            self._check_board_exists(url, board_id, typed_name=self.data.get("name", ""))
        return cleaned

    def _check_board_exists(self, url: str, board_id: str, *, typed_name: str) -> None:
        until = timezone.now()
        since = until - timedelta(hours=settings.EIOS_BACKFILL_HOURS)
        fetcher = EIOSFetcher(timeout=_BOARD_CHECK_TIMEOUT_SECONDS)
        try:
            self.recent_articles = fetcher.board_article_count(url, since=since, until=until)
        except (RuntimeError, httpx.HTTPError, ValueError, KeyError) as exc:
            self.check_warning = (
                f"Could not check this board with EIOS ({type(exc).__name__}), so it was saved "
                "unchecked. If its crawl fails, check the board ID."
            )
            return
        finally:
            fetcher.close()
        if self.recent_articles is None:
            typed_name = typed_name.strip()
            swapped = typed_name.isdigit() and not board_id.isdigit()
            hint = (
                f" It looks like the two boxes may be swapped: try “{typed_name}” as the "
                f"board ID and “{board_id}” as the name."
                if swapped
                else " Board IDs are usually long numbers, not the board's name."
            )
            self.add_error("board", f"EIOS has no board with the ID “{board_id}”.{hint}")

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
            notes=f"WHO-EIOS board {board_id} via the service-to-service API.",
        )
