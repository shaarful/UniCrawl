import json
from collections import Counter
from urllib.parse import urlsplit

from django.contrib import admin, messages
from django.utils.formats import date_format
from django.utils.html import format_html

from .models import RawArticle

_PRE = '<pre class="article-pre">{}</pre>'

# Pages are fetched in parallel, but a bulk refetch still runs inside the
# admin request, so the selection is capped to keep it to a few minutes.
MAX_BULK_REFETCH = 100

_NEW_TAB_LINK = '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>'


def _is_web_link(url: str) -> bool:
    return urlsplit(url or "").scheme.lower() in ("http", "https")


def _external_link(url: str, text: str | None = None):
    """A link only for http(s). URLs come from outside sources, and a feed could carry
    a javascript: one that would run in the signed-in user's session if clicked."""
    if not _is_web_link(url):
        return url or ""
    return format_html(_NEW_TAB_LINK, url, text or url)


def _short_date(value) -> str | None:
    return date_format(value, "M j, Y") if value else None


def _status_label(article: RawArticle) -> tuple[str, str]:
    """A pill's style and wording. Unmatched articles rest at pending for good, so
    the model's "Pending" would wrongly suggest that something is still to come."""
    status = RawArticle.ProcessingStatus
    if article.processing_status == status.PENDING and not article.is_keyword_matched:
        return "off", "No keyword match"
    return {
        status.PENDING: ("off", "Pending"),
        status.MATCHED: ("warn", "Keyword matched"),
        status.SUPPRESSED: ("off", "Duplicate"),
        status.PUBLISHED: ("ok", "Sent to LLM"),
        status.FAILED: ("bad", "Failed"),
    }.get(article.processing_status, ("off", article.get_processing_status_display()))


@admin.register(RawArticle)
class RawArticleAdmin(admin.ModelAdmin):
    actions = ["refetch_from_original_link"]
    list_per_page = 20
    # "Show all" appears up to this many rows, so a large list must be filtered first:
    # all 6,454 dev articles took about 4 s to render, and this grows with the row count.
    list_max_show_all = 10_000
    # Kept narrow: beside the menu, more columns would hide the last ones behind a scrollbar.
    list_display = ("article", "status", "diseases", "lang", "published", "open_link")
    list_display_links = ("article",)
    list_select_related = ("source",)
    list_filter = ("processing_status", "is_keyword_matched", "is_duplicate",
                   "language", "source_type", "source")
    search_fields = ("url", "title", "body", "author", "source_name")
    readonly_fields = (
        "source", "url_link", "final_url_link", "http_status", "fetched_at",
        "title", "body", "language",
        "published_at", "author", "source_name", "source_type",
        "url_fingerprint", "content_hash",
        "is_duplicate", "duplicate_of", "suppression_reason",
        "is_keyword_matched", "matched_diseases",
        "body_source", "link_error", "crawled_text", "metadata_json",
        "created_at",
    )
    fieldsets = (
        (None, {"fields": ("source", "language", "url_link", "final_url_link")}),
        ("Text", {"fields": ("body",)}),
        ("Source-stated fields", {"fields": ("published_at", "author",
                                             "source_name", "source_type")}),
        ("Pipeline", {"fields": ("processing_status", "is_keyword_matched", "matched_diseases",
                                 "is_duplicate", "duplicate_of", "suppression_reason")}),
        ("Crawl details", {"classes": ("collapse",),
                           "fields": ("http_status", "fetched_at", "created_at", "body_source",
                                      "link_error", "crawled_text", "metadata_json")}),
        ("Fingerprints", {"classes": ("collapse",),
                          "fields": ("url_fingerprint", "content_hash")}),
    )

    def has_add_permission(self, request):
        # Articles only come from crawling, and every field here is read-only.
        return False

    def get_fieldsets(self, request, obj=None):
        if obj is not None and obj.final_url and obj.final_url != obj.url:
            return self.fieldsets
        first_name, first = self.fieldsets[0]
        fields = tuple(name for name in first["fields"] if name != "final_url_link")
        return ((first_name, {**first, "fields": fields}), *self.fieldsets[1:])

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), "title": "Articles"})

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj is not None:
            context.update(title="Article", subtitle=obj.title or obj.url)
        return super().render_change_form(request, context, add, change, form_url, obj)

    @admin.display(description="Article", ordering="title")
    def article(self, obj: RawArticle):
        details = [
            obj.source_name or obj.source.name,
            urlsplit(obj.url).netloc,
            self.body_source(obj),
        ]
        return format_html(
            '<span class="article-title">{}</span><span class="article-from">{}</span>',
            obj.title or obj.url, " · ".join(part for part in details if part),
        )

    @admin.display(description="Status", ordering="processing_status")
    def status(self, obj: RawArticle):
        kind, label = _status_label(obj)
        return format_html('<span class="status-pill {}">{}</span>', kind, label)

    @admin.display(description="Diseases")
    def diseases(self, obj: RawArticle) -> str:
        return ", ".join(obj.matched_diseases or [])

    @admin.display(description="Lang", ordering="language")
    def lang(self, obj: RawArticle) -> str:
        return obj.language

    @admin.display(description="Published", ordering="published_at")
    def published(self, obj: RawArticle):
        return format_html(
            '{}<span class="article-from">Fetched {}</span>',
            _short_date(obj.published_at) or "–", _short_date(obj.fetched_at),
        )

    @admin.display(description="")
    def open_link(self, obj: RawArticle):
        return _external_link(obj.url, "Open ↗︎") if _is_web_link(obj.url) else ""

    @admin.display(description="URL")
    def url_link(self, obj: RawArticle):
        return _external_link(obj.url)

    @admin.display(description="URL after redirects")
    def final_url_link(self, obj: RawArticle):
        return _external_link(obj.final_url)

    @admin.action(description="Refetch selected articles from their original links")
    def refetch_from_original_link(self, request, queryset):
        from crawler.refetch import refetch_articles

        count = queryset.count()
        if count > MAX_BULK_REFETCH:
            self.message_user(
                request,
                f"{count:,} articles selected — refetch at most {MAX_BULK_REFETCH} at a time "
                "(narrow the selection with the filters).",
                messages.ERROR,
            )
            return
        results = list(refetch_articles(list(queryset.values_list("pk", flat=True))).values())

        read = [r for r in results if r.ok]
        summary = f"Refetched {len(read)} of {len(results)} articles"
        details = [
            f"{sum(r.body_changed for r in read)} got new text from the page",
            f"{sum(r.published for r in read)} sent to the LLM queue",
        ]
        unsent = sum("not sent to the LLM queue" in r.message for r in read)
        if unsent:
            details.append(f"{unsent} matched but not sent (queue unavailable)")
        if read:
            self.message_user(request, f"{summary}: {'; '.join(details)}.", messages.SUCCESS)

        failures = Counter(r.error for r in results if not r.ok)
        if failures:
            reasons = ", ".join(f"{reason} ×{n}" for reason, n in failures.most_common())
            self.message_user(
                request,
                f"{sum(failures.values())} could not be read: {reasons}.",
                messages.WARNING,
            )

    @admin.display(description="Body source")
    def body_source(self, obj: RawArticle) -> str:
        return (obj.metadata or {}).get("body_source", "")

    @admin.display(description="Why snippet / link error")
    def link_error(self, obj: RawArticle) -> str:
        return (obj.metadata or {}).get("link_error", "")

    @admin.display(description="Crawled page text")
    def crawled_text(self, obj: RawArticle):
        text = (obj.metadata or {}).get("crawled_text")
        return format_html(_PRE, text) if text else ""

    @admin.display(description="All metadata (incl. EIOS record)")
    def metadata_json(self, obj: RawArticle):
        if not obj.metadata:
            return ""
        return format_html(_PRE, json.dumps(obj.metadata, indent=2, ensure_ascii=False))
