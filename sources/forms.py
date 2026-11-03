"""The admin form for adding and editing a source."""
from __future__ import annotations

import math

from django import forms
from django.conf import settings

from .models import Source

DEPTH_CHOICES = [
    (1, "A single article"),
    (2, "A list of articles: follow its links"),
    (3, "A list of articles: also follow links on those pages"),
]


def interval_minutes(seconds: int) -> int:
    """Whole minutes for display, never below the one-minute form minimum."""
    return max(1, math.ceil(seconds / 60))


def keywords_as_text(keywords) -> str:
    return "\n".join(str(keyword) for keyword in keywords) if isinstance(keywords, list) else ""


class SourceForm(forms.ModelForm):
    """Asks for minutes and one keyword per line, and stores what the crawler reads."""

    crawl_interval_minutes = forms.IntegerField(
        label="Crawl every (minutes)",
        min_value=1,
        help_text="60 is once an hour and 1440 is once a day.",
    )
    crawl_depth = forms.TypedChoiceField(
        label="This URL is",
        choices=DEPTH_CHOICES,
        coerce=int,
        help_text="Only used for News / HTML sources. Feeds list their own articles.",
    )
    keywords_text = forms.CharField(
        label="Keywords for this source only",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text="One per line. Leave empty to use the shared disease keywords. "
                  "When filled in, these replace the shared keywords for this source.",
    )

    class Meta:
        model = Source
        fields = ("name", "url", "content_type", "language_hint", "notes", "crawl_depth",
                  "is_active")
        labels = {
            "url": "URL",
            "content_type": "Source type",
            "language_hint": "Language",
            "is_active": "Crawl on schedule",
        }
        help_texts = {
            "content_type": "To add a WHO-EIOS board, use Add EIOS board on the crawl dashboard.",
            "language_hint": "A code such as en, hi or bn. Leave empty to detect it automatically.",
            "is_active": "Turn on after a good test run. "
                         "Scheduled crawls also need the Celery worker and beat running.",
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            # The admin's own URL widget adds a Currently/Change line; Open site covers that.
            "url": forms.URLInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        saved = self.instance.pk is not None
        minutes = interval_minutes(
            self.instance.crawl_interval_seconds
            if saved
            else settings.CRAWLER_DEFAULT_INTERVAL_SECONDS
        )
        for name, value in (
            ("crawl_interval_minutes", minutes),
            ("keywords_text", keywords_as_text(self.instance.keyword_filter_overrides)),
        ):
            if name in self.fields:
                self.fields[name].initial = value

        if "crawl_depth" in self.fields and saved:
            # A depth set through the API can be outside the usual three choices.
            depth = self.instance.crawl_depth
            if depth not in dict(DEPTH_CHOICES):
                self.fields["crawl_depth"].choices = [
                    *DEPTH_CHOICES, (depth, f"Follow links {depth} levels deep"),
                ]
        for name, placeholder in (
            ("name", "IEDCR outbreak bulletin"),
            ("url", "https://example.org/news"),
            ("language_hint", "bn"),
        ):
            if name in self.fields:
                self.fields[name].widget.attrs["placeholder"] = placeholder

    def clean_keywords_text(self) -> list[str]:
        keywords = []
        for line in self.cleaned_data["keywords_text"].splitlines():
            keyword = line.strip()
            if keyword and keyword not in keywords:
                keywords.append(keyword)
        return keywords

    def save(self, commit=True):
        # Only touched when edited, so an interval that is not whole minutes and an
        # existing empty keyword list are not rewritten by an unrelated change.
        adding = self.instance.pk is None
        if adding or "crawl_interval_minutes" in self.changed_data:
            self.instance.crawl_interval_seconds = (
                self.cleaned_data["crawl_interval_minutes"] * 60
            )
        if adding or "keywords_text" in self.changed_data:
            self.instance.keyword_filter_overrides = self.cleaned_data["keywords_text"] or None
        return super().save(commit=commit)
