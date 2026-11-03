"""Django admin registration for source management."""
from django.contrib import admin, messages
from django.db.models import Count
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.timesince import timesince

from unicrawl.admin_display import pill

from .forms import SourceForm, interval_minutes, keywords_as_text
from .models import Source

_SHORT_TYPES = {
    Source.ContentType.HTML: "News / HTML",
    Source.ContentType.RSS: "RSS feed",
    Source.ContentType.EIOS: "WHO-EIOS",
}


def _every(seconds: int) -> str:
    if seconds % 86400 == 0:
        days = seconds // 86400
        return "Every day" if days == 1 else f"Every {days} days"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    minutes = interval_minutes(seconds)
    return "Every minute" if minutes == 1 else f"Every {minutes} minutes"


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    form = SourceForm
    # Kept narrow: beside the menu and filters, wider columns push Run now off the screen.
    list_display = ("source", "source_type", "status", "last_crawled", "articles", "run_now")
    list_display_links = ("source",)
    list_filter = ("content_type", "is_active", "is_temporarily_unavailable")
    search_fields = ("name", "url")
    actions = ("start_scheduling", "stop_scheduling")
    readonly_fields = ("last_crawled_at", "next_crawl_at", "consecutive_failures",
                       "is_temporarily_unavailable", "unavailable_until",
                       "added_by", "created_at", "updated_at")

    def get_fieldsets(self, request, obj=None):
        crawling = ["crawl_interval_minutes", "crawl_depth", "keywords_text"]
        if obj is None:
            # A new source starts unscheduled: it gets a test run first.
            return (
                (None, {"fields": ("name", "url", "content_type", "language_hint", "notes")}),
                ("Crawling", {"fields": crawling}),
            )
        return (
            (None, {"fields": ("name", "url", "content_type", "language_hint", "notes")}),
            ("Crawling", {"fields": [*crawling, "is_active"]}),
            ("Status", {"fields": ("last_crawled_at", "next_crawl_at", "consecutive_failures",
                                   "is_temporarily_unavailable", "unavailable_until")}),
            ("History", {"classes": ("collapse",),
                         "fields": ("added_by", "created_at", "updated_at")}),
        )

    def get_list_display(self, request):
        if request.user.has_perm("raw_content.delete_rawarticle"):
            return tuple("run_buttons" if c == "run_now" else c for c in self.list_display)
        return self.list_display

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(article_count=Count("raw_articles"))

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), "title": "Sources"})

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        super().save_model(request, obj, form, change)

    def response_add(self, request, obj, post_url_continue=None):
        """Plain Save goes on to the new source, where its test run starts."""
        if any(key in request.POST for key in ("_popup", "_addanother", "_continue")):
            return super().response_add(request, obj, post_url_continue)
        self.message_user(
            request,
            f"Added {obj.name}. Press Run now to test it, then turn on Crawl on schedule.",
            messages.SUCCESS,
        )
        return redirect("admin:sources_source_change", obj.pk)

    # Form-only fields also need these, because a view-only user sees them as plain values.
    @admin.display(description="Crawl every (minutes)")
    def crawl_interval_minutes(self, obj: Source) -> int:
        return interval_minutes(obj.crawl_interval_seconds)

    @admin.display(description="Keywords for this source only")
    def keywords_text(self, obj: Source) -> str:
        return keywords_as_text(obj.keyword_filter_overrides)

    @admin.display(description="Source", ordering="name")
    def source(self, obj: Source):
        return format_html(
            '<span class="source-name">{}</span><span class="source-url">{}</span>',
            obj.name, obj.url,
        )

    @admin.display(description="Type", ordering="content_type")
    def source_type(self, obj: Source) -> str:
        return _SHORT_TYPES.get(obj.content_type, obj.get_content_type_display())

    @admin.display(description="Status")
    def status(self, obj: Source):
        if obj.is_temporarily_unavailable:
            kind, label = "bad", "Unavailable"
        elif obj.consecutive_failures:
            count = obj.consecutive_failures
            kind, label = "warn", f"{count} failed {'try' if count == 1 else 'tries'}"
        elif obj.is_active:
            kind, label = "ok", "Scheduled"
        else:
            kind, label = "off", "Not scheduled"
        return format_html(
            '{}<span class="source-every">{}</span>',
            pill(kind, label), _every(obj.crawl_interval_seconds),
        )

    @admin.display(description="Last crawled", ordering="last_crawled_at", empty_value="Never")
    def last_crawled(self, obj: Source) -> str | None:
        return f"{timesince(obj.last_crawled_at)} ago" if obj.last_crawled_at else None

    @admin.display(description="Articles", ordering="article_count")
    def articles(self, obj: Source):
        url = reverse("admin:raw_content_rawarticle_changelist")
        return format_html(
            '<a href="{}?source__id__exact={}">{}</a>', url, obj.pk, f"{obj.article_count:,}",
        )

    @admin.display(description="")
    def run_now(self, obj: Source):
        # The list is already one form, so the button posts that form to the run URL.
        return format_html(
            '<button type="submit" class="button source-run" formmethod="post" '
            'formaction="{}">Run now</button>',
            reverse("crawler:start", args=[obj.pk]),
        )

    @admin.display(description="")
    def run_buttons(self, obj: Source):
        return format_html(
            '<span class="source-buttons">{}<a class="button source-recrawl" href="{}" '
            'title="Delete all articles from this source and crawl it again">'
            "Delete &amp; re-crawl</a></span>",
            self.run_now(obj),
            reverse("crawler:source-recrawl", args=[obj.pk]),
        )

    @admin.action(description="Start scheduled crawls for selected sources", permissions=["change"])
    def start_scheduling(self, request, queryset):
        count = queryset.update(is_active=True, updated_at=timezone.now())
        self.message_user(request, f"{count} sources will now crawl on schedule.", messages.SUCCESS)

    @admin.action(description="Stop scheduled crawls for selected sources", permissions=["change"])
    def stop_scheduling(self, request, queryset):
        count = queryset.update(is_active=False, updated_at=timezone.now())
        self.message_user(
            request, f"{count} sources will no longer crawl on schedule.", messages.SUCCESS,
        )
