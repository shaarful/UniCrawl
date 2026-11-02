"""REST API views for source CRUD.

A test-crawl + accessibility check must happen on create before activating the
source — to be added once fetchers and pipeline are wired up.
"""
from rest_framework import viewsets

from .models import Source
from .serializers import SourceSerializer


class SourceViewSet(viewsets.ModelViewSet):
    queryset = Source.objects.all()
    serializer_class = SourceSerializer
