import json

import redis
from django.contrib import admin, messages
from django.utils.html import format_html, format_html_join

from raw_content.admin import RawArticleAdmin
from raw_content.models import RawArticle

from .models import ArticleExtraction, ExtractedEvent

NOT_STATED = "–"


def _place(record: dict) -> str:
    parts = (record["village"], record["district"], record["division"], record["country"])
    return ", ".join(part for part in parts if part) or NOT_STATED


def _count(value: int | None):
    # Zero is a stated value, unlike null.
    return NOT_STATED if value is None else value


class ArticleExtractionInline(admin.StackedInline):
    """The LLM result, shown on the article's own page."""

    model = ArticleExtraction
    can_delete = False
    extra = 0
    verbose_name = "LLM result"
    verbose_name_plural = "LLM result"
    readonly_fields = (
        "status", "is_disease_event", "summary", "extracted_events", "translated_title",
        "translated_body", "input_truncated", "attempts", "error", "llm_model",
        "prompt_version", "prompt_tokens", "response_tokens", "updated_at",
    )

    def get_fieldsets(self, request, obj=None):
        """`obj` is the article. Fields with nothing to say are left out, and an error,
        which explains a failed result, stays in view."""
        extraction = getattr(obj, "extraction", None)
        main = ["status", "is_disease_event", "summary", "extracted_events"]
        if extraction is not None and extraction.error:
            main.append("error")
        if extraction is not None and extraction.translated_title:
            main.append("translated_title")
        if extraction is not None and extraction.translated_body:
            main.append("translated_body")
        return (
            (None, {"fields": main}),
            ("Processing details", {
                "classes": ("collapse",),
                "fields": ("llm_model", "prompt_version", "prompt_tokens", "response_tokens",
                           "input_truncated", "attempts", "updated_at"),
            }),
        )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Extracted events")
    def extracted_events(self, obj: ArticleExtraction):
        records = [event.as_record() for event in obj.events.all()]
        if not records:
            return "–"
        return format_html(
            '<div class="events-wrap"><table class="events-table"><thead><tr>'
            "<th>Disease</th><th>Host</th><th>Place</th><th>Event date</th>"
            '<th class="num">Confirmed</th><th class="num">Suspected</th>'
            '<th class="num">Deaths</th></tr></thead><tbody>{}</tbody></table></div>'
            '<details class="events-json"><summary>Full record (JSON)</summary>'
            '<pre class="article-pre">{}</pre></details>',
            format_html_join(
                "",
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
                '<td class="num">{}</td><td class="num">{}</td><td class="num">{}</td></tr>',
                (
                    (
                        record["disease_name"],
                        record["host"] or NOT_STATED,
                        _place(record),
                        record["event_date"] or NOT_STATED,
                        _count(record["confirmed_case_count"]),
                        _count(record["suspected_case_count"]),
                        _count(record["fatality_count"]),
                    )
                    for record in records
                ),
            ),
            json.dumps(records, indent=2, ensure_ascii=False),
        )


class RawArticleLlmAdmin(RawArticleAdmin):
    """Adds the LLM actions here, so raw_content does not depend on this app."""

    change_form_template = "admin/llm/rawarticle_change_form.html"
    inlines = [*RawArticleAdmin.inlines, ArticleExtractionInline]
    actions = [*RawArticleAdmin.actions, "send_to_llm_queue"]
    # After the crawler's status, and before the dates and link.
    list_display = [*RawArticleAdmin.list_display[:2], "llm_status",
                    *RawArticleAdmin.list_display[2:]]
    list_select_related = ("source", "extraction")

    @admin.display(description="LLM status", ordering="extraction__status")
    def llm_status(self, obj: RawArticle):
        extraction = getattr(obj, "extraction", None)  # articles not processed yet have none
        if extraction is None:
            kind, label = "off", "Not processed"
        else:
            kind = {
                ArticleExtraction.Status.COMPLETED: "ok",
                ArticleExtraction.Status.RETRYING: "warn",
                ArticleExtraction.Status.FAILED: "bad",
            }.get(extraction.status, "off")
            label = extraction.get_status_display()
        return format_html('<span class="status-pill {}">{}</span>', kind, label)

    @admin.action(description="Send selected matched articles to the LLM queue")
    def send_to_llm_queue(self, request, queryset):
        from crawler.publisher import publish_matched

        eligible = queryset.filter(is_keyword_matched=True, is_duplicate=False)
        sent = 0
        try:
            for article in eligible:
                publish_matched(article.pk)
                article.processing_status = RawArticle.ProcessingStatus.PUBLISHED
                article.save(update_fields=["processing_status"])
                sent += 1
        except redis.RedisError as exc:
            self.message_user(
                request,
                f"Sent {sent} before the LLM queue became unavailable: {exc}",
                messages.ERROR,
            )
            return
        skipped = queryset.count() - sent
        note = f" Skipped {skipped} duplicate or unmatched." if skipped else ""
        self.message_user(
            request, f"Sent {sent} articles to the LLM queue.{note}", messages.SUCCESS,
        )


admin.site.unregister(RawArticle)
admin.site.register(RawArticle, RawArticleLlmAdmin)


class ExtractedEventInline(admin.TabularInline):
    model = ExtractedEvent
    extra = 0
    can_delete = False
    fields = readonly_fields = (
        "disease", "host", "country", "division", "district", "village", "location_type",
        "event_date", "confirmed_case_count", "suspected_case_count", "fatality_count",
        "affected_population_size", "response_action", "responding_authority",
    )

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ArticleExtraction)
class ArticleExtractionAdmin(admin.ModelAdmin):
    """Read-only view: results only ever come from the worker."""

    inlines = [ExtractedEventInline]
    list_display = ("article", "status", "is_disease_event", "attempts", "llm_model",
                    "prompt_tokens", "response_tokens", "updated_at")
    list_filter = ("status", "is_disease_event", "llm_model", "prompt_version")
    list_select_related = ("article",)
    search_fields = ("article__title", "article__url", "events__disease", "events__country")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(ExtractedEvent)
class ExtractedEventAdmin(admin.ModelAdmin):
    list_display = ("disease", "host", "country", "district", "event_date",
                    "confirmed_case_count", "suspected_case_count", "fatality_count", "extraction")
    list_filter = ("disease", "host", "country", "location_type")
    list_select_related = ("extraction",)
    search_fields = ("disease", "host", "country", "division", "district", "village")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
