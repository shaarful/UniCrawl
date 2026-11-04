from datetime import datetime

from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from core.admin_display import pill, short_datetime, titled, with_note
from crawler.runs import mark_stale_runs

from .models import CrawlRun

_STATUS_KINDS = {
    CrawlRun.Status.RUNNING: "warn",
    CrawlRun.Status.SUCCEEDED: "ok",
    CrawlRun.Status.FAILED: "bad",
    CrawlRun.Status.CANCELLED: "off",
    CrawlRun.Status.INTERRUPTED: "bad",
}
_LEVEL_KINDS = {"warning": "warn", "error": "bad"}


def _duration(run: CrawlRun) -> str:
    end = run.finished_at or timezone.now()
    seconds = max(0, round((end - run.started_at).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _clock(value) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return str(value or "")


@admin.register(CrawlRun)
class CrawlRunAdmin(admin.ModelAdmin):
    """Read-only history: runs are only ever written by the crawler."""

    list_display = (
        "run", "state", "processed", "new", "matched", "sent", "dups", "failures", "started",
        "live_link",
    )
    list_display_links = ("run",)
    list_select_related = ("source", "started_by")
    list_filter = ("status", "trigger", "source")
    search_fields = ("source__name", "stage", "error")
    readonly_fields = [
        *(f.name for f in CrawlRun._meta.fields), "state", "duration", "timeline",
    ]

    def get_fieldsets(self, request, obj=None):
        overview = ["source", "state", "trigger", "started_by", "started_at", "finished_at",
                    "duration", "stage"]
        if obj is not None and obj.error:
            overview.append("error")
        return (
            (None, {"fields": overview}),
            ("Results", {"fields": ("processed", "new", "matched", "published", "duplicates",
                                    "skipped", "full_text", "errors")}),
            ("Timeline", {"fields": ("timeline",)}),
            ("Technical details", {
                "classes": ("collapse",),
                "fields": ("percent", "fetched_at", "heartbeat_at", "cancel_requested"),
            }),
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # A running crawl keeps writing to its row.
        if obj is not None and obj.is_running:
            return False
        return super().has_delete_permission(request, obj)

    def changelist_view(self, request, extra_context=None):
        mark_stale_runs()  # so a crawl whose process died does not look like it is running
        return super().changelist_view(request, {**(extra_context or {}), "title": "Crawl runs"})

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj is not None:
            context.update(title="Crawl run", subtitle=f"Run #{obj.pk} · {obj.source.name}")
        return super().render_change_form(request, context, add, change, form_url, obj)

    @admin.display(description="Run", ordering="source__name")
    def run(self, obj: CrawlRun):
        started_by = f" by {obj.started_by.get_username()}" if obj.started_by else ""
        return titled(
            f"#{obj.pk} {obj.source.name}", f"{obj.get_trigger_display()}{started_by}",
        )

    @admin.display(description="Status", ordering="status")
    def state(self, obj: CrawlRun):
        return pill(_STATUS_KINDS.get(obj.status, "off"), obj.get_status_display())

    @admin.display(description="Sent", ordering="published")
    def sent(self, obj: CrawlRun) -> int:
        return obj.published

    @admin.display(description="Dups", ordering="duplicates")
    def dups(self, obj: CrawlRun) -> int:
        return obj.duplicates

    @admin.display(description="Errors", ordering="errors")
    def failures(self, obj: CrawlRun):
        return pill("bad", f"{obj.errors:,}") if obj.errors else 0

    @admin.display(description="Started", ordering="started_at")
    def started(self, obj: CrawlRun):
        verb = "Running for" if obj.is_running else "Took"
        return with_note(short_datetime(obj.started_at), f"{verb} {_duration(obj)}")

    @admin.display(description="Duration")
    def duration(self, obj: CrawlRun) -> str:
        return _duration(obj)

    @admin.display(description="")
    def live_link(self, obj: CrawlRun):
        return format_html('<a href="{}">Live page</a>', reverse("crawler:run", args=[obj.pk]))

    @admin.display(description="What happened")
    def timeline(self, obj: CrawlRun):
        entries = [entry for entry in (obj.events or []) if isinstance(entry, dict)]
        if not entries:
            return "–"
        return format_html(
            '<div class="events-wrap"><table class="events-table"><thead><tr>'
            "<th>Time (UTC)</th><th>Level</th><th>Message</th></tr></thead>"
            "<tbody>{}</tbody></table></div>",
            format_html_join(
                "",
                '<tr><td class="nowrap">{}</td><td>{}</td><td class="wrap">{}</td></tr>',
                (
                    (
                        _clock(entry.get("t")),
                        pill(
                            _LEVEL_KINDS.get(entry.get("level"), "off"),
                            entry.get("level", "info"),
                        ),
                        entry.get("msg", ""),
                    )
                    for entry in entries
                ),
            ),
        )
