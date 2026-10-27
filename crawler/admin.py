from django.contrib import admin

from .models import CrawlRun


@admin.register(CrawlRun)
class CrawlRunAdmin(admin.ModelAdmin):
    list_display = (
        "id", "source", "trigger", "status", "processed", "new", "matched",
        "published", "duplicates", "skipped", "full_text", "errors", "started_at", "finished_at",
    )
    list_filter = ("status", "trigger", "source")
    readonly_fields = [f.name for f in CrawlRun._meta.fields]
