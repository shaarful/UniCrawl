import json

import redis
from django.contrib import admin, messages
from django.utils.html import format_html

from raw_content.admin import RawArticleAdmin
from raw_content.models import RawArticle

from .models import ArticleExtraction, ExtractedEvent


class ArticleExtractionInline(admin.StackedInline):
    """The LLM result, shown on the article's own page."""

    model = ArticleExtraction
    can_delete = False
    extra = 0
    verbose_name = "LLM result"
    verbose_name_plural = "LLM result"
    fields = readonly_fields = (
        "status", "is_disease_event", "summary", "extracted_events", "translated_title",
        "translated_body", "input_truncated", "attempts", "error", "llm_model",
        "prompt_version", "prompt_tokens", "response_tokens", "updated_at",
    )

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Extracted events (FR-09 records)")
    def extracted_events(self, obj: ArticleExtraction):
        records = [event.as_record() for event in obj.events.all()]
        if not records:
            return "–"
        return format_html(
            '<pre style="white-space: pre-wrap; margin: 0">{}</pre>',
            json.dumps(records, indent=2, ensure_ascii=False),
        )


class RawArticleLlmAdmin(RawArticleAdmin):
    """Adds the LLM actions here, so raw_content does not depend on this app."""

    change_form_template = "admin/llm/rawarticle_change_form.html"
    inlines = [*RawArticleAdmin.inlines, ArticleExtractionInline]
    actions = [*RawArticleAdmin.actions, "send_to_llm_queue"]
    list_display = [*RawArticleAdmin.list_display, "llm_status"]
    list_select_related = ("source", "extraction")

    @admin.display(description="LLM status", ordering="extraction__status")
    def llm_status(self, obj: RawArticle) -> str:
        extraction = getattr(obj, "extraction", None)  # articles not processed yet have none
        return extraction.get_status_display() if extraction else "Not processed"

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
