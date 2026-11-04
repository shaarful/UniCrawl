import json

import redis
from django.contrib import admin, messages
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from core.admin_display import pill, short_date, short_datetime, titled, with_note
from raw_content.admin import RawArticleAdmin
from raw_content.models import RawArticle

from .models import ArticleExtraction, ExtractedEvent

NOT_STATED = "–"
_STATUS_KINDS = {
    ArticleExtraction.Status.COMPLETED: "ok",
    ArticleExtraction.Status.RETRYING: "warn",
    ArticleExtraction.Status.FAILED: "bad",
}


def _place_of(event: ExtractedEvent) -> str:
    parts = (event.village, event.district, event.division, event.country)
    return ", ".join(part for part in parts if part) or NOT_STATED


def _count(value: int | None):
    # Zero is a stated value, unlike null.
    return NOT_STATED if value is None else value


def _article_title(article: RawArticle) -> str:
    return article.title or article.url


def _article_link(article: RawArticle):
    url = reverse("admin:raw_content_rawarticle_change", args=[article.pk])
    return format_html('<a href="{}">{}</a>', url, _article_title(article))


def _status_pill(extraction: ArticleExtraction):
    return pill(_STATUS_KINDS.get(extraction.status, "off"), extraction.get_status_display())


def _events_table(extraction: ArticleExtraction):
    events = list(extraction.events.all())
    if not events:
        return NOT_STATED
    return format_html(
        '<div class="events-wrap"><table class="events-table"><thead><tr>'
        "<th>Disease</th><th>Host</th><th>Place</th><th>Event date</th>"
        '<th class="num">Confirmed</th><th class="num">Suspected</th>'
        '<th class="num">Deaths</th></tr></thead><tbody>{}</tbody></table></div>'
        '<details class="events-json"><summary>Full record (JSON)</summary>'
        '<pre class="article-pre">{}</pre></details>',
        format_html_join(
            "",
            '<tr><td><a href="{}">{}</a></td><td>{}</td><td>{}</td><td>{}</td>'
            '<td class="num">{}</td><td class="num">{}</td><td class="num">{}</td></tr>',
            (
                (
                    reverse("admin:llm_extractedevent_change", args=[event.pk]),
                    event.disease,
                    event.host or NOT_STATED,
                    _place_of(event),
                    short_date(event.event_date) or NOT_STATED,
                    _count(event.confirmed_case_count),
                    _count(event.suspected_case_count),
                    _count(event.fatality_count),
                )
                for event in events
            ),
        ),
        json.dumps([event.as_record() for event in events], indent=2, ensure_ascii=False),
    )


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
        return _events_table(obj)


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
        return _status_pill(extraction) if extraction else pill("off", "Not processed")

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


@admin.register(ArticleExtraction)
class ArticleExtractionAdmin(admin.ModelAdmin):
    """Read-only view: results only ever come from the worker."""

    list_display = ("article_title", "result", "disease_event", "event_count", "short_summary",
                    "updated")
    list_display_links = ("article_title",)
    list_filter = ("status", "is_disease_event", "llm_model", "prompt_version")
    list_select_related = ("article", "article__source")
    search_fields = ("article__title", "article__url", "events__disease", "events__country")
    readonly_fields = (
        "article_link", "result", "disease_event", "summary", "events_table", "error",
        "translated_title", "translated_body", "reporter_author", "author_affiliation",
        "publication_date", "llm_model", "prompt_version", "prompt_tokens", "response_tokens",
        "input_truncated", "attempts", "input_hash", "stream_message_id", "raw_response_text",
        "created_at", "updated_at",
    )

    def get_fieldsets(self, request, obj=None):
        result = ["article_link", "result", "disease_event", "summary", "events_table"]
        if obj is not None and obj.error:
            result.append("error")
        fieldsets = [(None, {"fields": result})]
        if obj is not None and (obj.translated_title or obj.translated_body):
            fieldsets.append(("Translation", {"fields": ("translated_title", "translated_body")}))
        if obj is not None and (
            obj.reporter_author or obj.author_affiliation or obj.publication_date
        ):
            fieldsets.append(("Stated in the text", {
                "fields": ("reporter_author", "author_affiliation", "publication_date"),
            }))
        fieldsets.append(("Processing details", {
            "classes": ("collapse",),
            "fields": ("llm_model", "prompt_version", "prompt_tokens", "response_tokens",
                       "input_truncated", "attempts", "input_hash", "stream_message_id",
                       "created_at", "updated_at", "raw_response_text"),
        }))
        return fieldsets

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(events_total=Count("events"))

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(request, {**(extra_context or {}), "title": "LLM results"})

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj is not None:
            context.update(title="LLM result", subtitle=_article_title(obj.article))
        return super().render_change_form(request, context, add, change, form_url, obj)

    @admin.display(description="Article", ordering="article__title")
    def article_title(self, obj: ArticleExtraction):
        article = obj.article
        return titled(_article_title(article), article.source_name or article.source.name)

    @admin.display(description="Article")
    def article_link(self, obj: ArticleExtraction):
        return _article_link(obj.article)

    @admin.display(description="Status", ordering="status")
    def result(self, obj: ArticleExtraction):
        return _status_pill(obj)

    @admin.display(description="Relevance", ordering="is_disease_event")
    def disease_event(self, obj: ArticleExtraction):
        if obj.is_disease_event is None:
            return NOT_STATED
        return pill("ok", "Disease event") if obj.is_disease_event else pill("off", "Not an event")

    @admin.display(description="Events", ordering="events_total")
    def event_count(self, obj: ArticleExtraction) -> int:
        return obj.events_total

    @admin.display(description="Summary")
    def short_summary(self, obj: ArticleExtraction):
        return format_html('<span class="cell-clamp">{}</span>', obj.error or obj.summary)

    @admin.display(description="Updated", ordering="updated_at")
    def updated(self, obj: ArticleExtraction):
        if obj.prompt_tokens is None and obj.response_tokens is None:
            return short_datetime(obj.updated_at)
        tokens = f"{obj.prompt_tokens or 0:,} in, {obj.response_tokens or 0:,} out"
        return with_note(short_datetime(obj.updated_at), f"Tokens: {tokens}")

    @admin.display(description="Extracted events")
    def events_table(self, obj: ArticleExtraction):
        return _events_table(obj)

    @admin.display(description="Raw LLM response")
    def raw_response_text(self, obj: ArticleExtraction):
        if not obj.raw_response:
            return NOT_STATED
        return format_html('<pre class="article-pre">{}</pre>', obj.raw_response)


@admin.register(ExtractedEvent)
class ExtractedEventAdmin(admin.ModelAdmin):
    """Read-only view: events only ever come from the worker."""

    list_display = ("disease", "host", "place", "event_day", "confirmed", "suspected", "deaths",
                    "article")
    list_display_links = ("disease",)
    list_filter = ("disease", "host", "country", "location_type")
    list_select_related = ("extraction__article",)
    search_fields = ("disease", "host", "country", "division", "district", "village",
                     "extraction__article__title")
    readonly_fields = (
        "disease", "host", "location_type", "affected_population_size", "event_date",
        "confirmed_case_count", "suspected_case_count", "fatality_count",
        "village", "district", "division", "country", "latitude", "longitude",
        "response_action", "responding_authority",
        "article_link", "source_name", "published", "article_summary", "full_record",
    )
    fieldsets = (
        (None, {"fields": ("disease", "host", "location_type", "event_date",
                           "affected_population_size")}),
        ("Counts", {"fields": ("confirmed_case_count", "suspected_case_count",
                               "fatality_count")}),
        ("Place", {"fields": ("village", "district", "division", "country", "latitude",
                              "longitude")}),
        ("Response", {"fields": ("responding_authority", "response_action")}),
        ("Article", {"fields": ("article_link", "source_name", "published", "article_summary")}),
        ("Full record", {"classes": ("collapse",), "fields": ("full_record",)}),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(
            request, {**(extra_context or {}), "title": "Extracted events"},
        )

    def render_change_form(self, request, context, add=False, change=False, form_url="", obj=None):
        if obj is not None:
            context.update(title="Extracted event", subtitle=str(obj))
        return super().render_change_form(request, context, add, change, form_url, obj)

    @admin.display(description="Place")
    def place(self, obj: ExtractedEvent) -> str:
        return _place_of(obj)

    @admin.display(description="Event date", ordering="event_date")
    def event_day(self, obj: ExtractedEvent) -> str | None:
        return short_date(obj.event_date)

    @admin.display(description="Confirmed", ordering="confirmed_case_count")
    def confirmed(self, obj: ExtractedEvent):
        return _count(obj.confirmed_case_count)

    @admin.display(description="Suspected", ordering="suspected_case_count")
    def suspected(self, obj: ExtractedEvent):
        return _count(obj.suspected_case_count)

    @admin.display(description="Deaths", ordering="fatality_count")
    def deaths(self, obj: ExtractedEvent):
        return _count(obj.fatality_count)

    @admin.display(description="Article", ordering="extraction__article__title")
    def article(self, obj: ExtractedEvent):
        return format_html(
            '<span class="cell-clamp">{}</span>', _article_link(obj.extraction.article),
        )

    @admin.display(description="Article")
    def article_link(self, obj: ExtractedEvent):
        return _article_link(obj.extraction.article)

    @admin.display(description="Publisher")
    def source_name(self, obj: ExtractedEvent) -> str:
        return obj.extraction.article.source_name

    @admin.display(description="Published")
    def published(self, obj: ExtractedEvent) -> str | None:
        return short_date(obj.extraction.article.published_at)

    @admin.display(description="LLM summary of the article")
    def article_summary(self, obj: ExtractedEvent) -> str:
        return obj.extraction.summary

    @admin.display(description="Full record (JSON)")
    def full_record(self, obj: ExtractedEvent):
        return format_html(
            '<pre class="article-pre">{}</pre>',
            json.dumps(obj.as_record(), indent=2, ensure_ascii=False),
        )
