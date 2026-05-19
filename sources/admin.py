"""Django admin registration for source management (FR-09)."""
from django.contrib import admin

from .models import Source


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = (
        "name", "content_type", "is_active", "is_temporarily_unavailable",
        "crawl_interval_seconds", "last_crawled_at", "consecutive_failures",
    )
    list_filter = ("content_type", "is_active", "is_temporarily_unavailable")
    search_fields = ("name", "url")
    readonly_fields = ("last_crawled_at", "next_crawl_at", "consecutive_failures",
                       "is_temporarily_unavailable", "unavailable_until",
                       "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("name", "url", "content_type", "language_hint", "notes")}),
        ("Crawl config", {"fields": ("crawl_interval_seconds", "crawl_depth",
                                     "keyword_filter_overrides", "is_active")}),
        ("Crawl state", {"fields": ("last_crawled_at", "next_crawl_at")}),
        ("Back-off state", {"fields": ("consecutive_failures",
                                        "is_temporarily_unavailable", "unavailable_until")}),
        ("Audit", {"fields": ("added_by", "created_at", "updated_at")}),
    )
