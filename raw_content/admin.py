import json
from collections import Counter

from django.contrib import admin, messages
from django.utils.html import format_html

from .models import RawArticle

_PRE = '<pre style="white-space: pre-wrap; max-height: 32em; overflow: auto; margin: 0">{}</pre>'

# Pages are fetched in parallel, but a bulk refetch still runs inside the
# admin request, so the selection is capped to keep it to a few minutes.
MAX_BULK_REFETCH = 100


@admin.register(RawArticle)
class RawArticleAdmin(admin.ModelAdmin):
    actions = ["refetch_from_original_link"]
    list_per_page = 20
    # "Show all" appears up to this many rows, so a large list must be filtered first:
    # all 6,454 dev articles took about 4 s to render, and this grows with the row count.
    list_max_show_all = 10_000
    list_display = (
        "id", "source", "source_name", "processing_status", "is_keyword_matched",
        "is_duplicate", "language", "body_source", "published_at", "fetched_at",
    )
    list_filter = ("processing_status", "is_keyword_matched", "is_duplicate",
                   "language", "source_type", "source")
    search_fields = ("url", "title", "body", "author", "source_name")
    readonly_fields = (
        "source", "url", "final_url", "http_status", "fetched_at",
        "title", "body", "language",
        "published_at", "author", "source_name", "source_type",
        "url_fingerprint", "content_hash",
        "is_duplicate", "duplicate_of", "suppression_reason",
        "is_keyword_matched", "matched_diseases",
        "body_source", "link_error", "crawled_text", "metadata_json",
        "created_at",
    )
    fieldsets = (
        ("Provenance", {"fields": ("source", "url", "final_url",
                                   "http_status", "fetched_at")}),
        ("Content", {"fields": ("title", "body", "language")}),
        ("Source-stated fields", {"fields": ("published_at", "author",
                                             "source_name", "source_type")}),
        ("Metadata", {"fields": ("body_source", "link_error", "crawled_text",
                                 "metadata_json")}),
        ("Dedup", {"fields": ("url_fingerprint", "content_hash",
                              "is_duplicate", "duplicate_of", "suppression_reason")}),
        ("Keyword match", {"fields": ("is_keyword_matched", "matched_diseases")}),
        ("Pipeline", {"fields": ("processing_status",)}),
        ("Audit", {"fields": ("created_at",)}),
    )

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
