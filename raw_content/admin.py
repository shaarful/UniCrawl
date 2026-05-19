from django.contrib import admin

from .models import RawArticle


@admin.register(RawArticle)
class RawArticleAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source", "processing_status", "is_keyword_matched",
        "is_duplicate", "language", "fetched_at",
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
        "created_at",
    )
    fieldsets = (
        ("Provenance", {"fields": ("source", "url", "final_url",
                                   "http_status", "fetched_at")}),
        ("Content", {"fields": ("title", "body", "language")}),
        ("Dedup", {"fields": ("url_fingerprint", "content_hash",
                              "is_duplicate", "duplicate_of", "suppression_reason")}),
        ("Keyword match", {"fields": ("is_keyword_matched", "matched_diseases")}),
        ("Pipeline", {"fields": ("processing_status",)}),
        ("Audit", {"fields": ("created_at",)}),
    )
