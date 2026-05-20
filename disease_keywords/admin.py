from django.contrib import admin

from .models import DiseaseKeyword


@admin.register(DiseaseKeyword)
class DiseaseKeywordAdmin(admin.ModelAdmin):
    list_display = ["disease_name", "keyword", "language", "is_active"]
    list_filter = ["disease_name", "is_active", "language"]
    search_fields = ["keyword", "disease_name"]
    list_editable = ["is_active"]
