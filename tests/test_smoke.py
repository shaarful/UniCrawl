"""Smoke tests: project must import cleanly and the health endpoint must respond."""
from __future__ import annotations

import importlib

import pytest
from django.test import Client


MODULES = [
    "core.settings.dev",
    "core.urls",
    "core.celery",
    "sources.models",
    "sources.serializers",
    "sources.views",
    "raw_content.models",
    "crawler.scheduler",
    "crawler.pipeline",
    "crawler.publisher",
    "crawler.fetchers.base",
    "crawler.fetchers.html",
    "crawler.fetchers.rss",
    "crawler.fetchers.eios",
    "crawler.filters.keyword",
    "crawler.dedup.url_fingerprint",
    "crawler.dedup.content_hash",
    "crawler.dedup.semantic",
    "crawler.extractors.base",
    "health.views",
]


@pytest.mark.parametrize("module_path", MODULES)
def test_module_imports(module_path: str) -> None:
    importlib.import_module(module_path)


@pytest.mark.django_db
def test_liveness_endpoint() -> None:
    client = Client()
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
