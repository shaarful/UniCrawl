from django.contrib import admin

from .models import ArticleExtraction, ExtractedEvent


class ExtractedEventInline(admin.TabularInline):
    model = ExtractedEvent
    extra = 0
    can_delete = False
    readonly_fields = ("disease", "country", "location", "number_of_cases")

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
    list_display = ("disease", "country", "location", "number_of_cases", "extraction")
    list_filter = ("disease", "country")
    list_select_related = ("extraction",)
    search_fields = ("disease", "country", "location")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
