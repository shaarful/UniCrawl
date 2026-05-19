"""DRF serializers for the Source REST API (FR-09)."""
from rest_framework import serializers

from .models import Source


class SourceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Source
        fields = "__all__"
