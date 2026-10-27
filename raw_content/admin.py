import json

from django.contrib import admin
from django.utils.html import format_html

from .models import RawArticle

_PRE = '<pre style="white-space: pre-wrap; max-height: 32em; overflow: auto; margin: 0">{}</pre>'


@admin.register(RawArticle)
class RawArticleAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source", "processing_status", "is_keyword_matched",
        "is_duplicate", "language", "body_source", "fetched_at",
    )
    list_filter = ("processing_status", "is_keyword_matched", "is_duplicate",
                   "language", "source")
    search_fields = ("url", "title", "body")
    readonly_fields = (
        "source", "url", "final_url", "http_status", "fetched_at",
        "title", "body", "language",
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
        ("Metadata", {"fields": ("body_source", "link_error", "crawled_text",
                                 "metadata_json")}),
        ("Dedup", {"fields": ("url_fingerprint", "content_hash",
                              "is_duplicate", "duplicate_of", "suppression_reason")}),
        ("Keyword match", {"fields": ("is_keyword_matched", "matched_diseases")}),
        ("Pipeline", {"fields": ("processing_status",)}),
        ("Audit", {"fields": ("created_at",)}),
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
