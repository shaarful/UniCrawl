"""Django admin pages for raw articles."""
from __future__ import annotations

import pytest
from django.urls import reverse
from django.utils import timezone

from raw_content.models import RawArticle
from sources.models import Source


def _article() -> RawArticle:
    source = Source.objects.create(name="EIOS", url="https://eios.example.org/board",
                                   content_type=Source.ContentType.EIOS)
    return RawArticle.objects.create(
        source=source, url="https://news.example.com/a", http_status=200,
        fetched_at=timezone.now(), title="Ebola update", body="Body text.",
        url_fingerprint="f", content_hash="h",
        metadata={
            "eios": {"id": "eios-1", "description": "Résumé EIOS"},
            "body_source": "eios_snippet",
            "link_error": "HTTP 403",
            "crawled_text": "Crawled <b>page</b> text",
        },
    )


@pytest.mark.django_db
def test_raw_article_change_page_shows_metadata(admin_client):
    article = _article()

    page = admin_client.get(reverse("admin:raw_content_rawarticle_change", args=[article.pk]))
    html = page.content.decode()

    assert page.status_code == 200
    assert "eios_snippet" in html
    assert "HTTP 403" in html
    assert "Crawled &lt;b&gt;page&lt;/b&gt; text" in html  # escaped, not rendered
    assert "Résumé EIOS" in html and "eios-1" in html


@pytest.mark.django_db
def test_raw_article_list_shows_body_source(admin_client):
    _article()
    page = admin_client.get(reverse("admin:raw_content_rawarticle_changelist"))
    html = page.content.decode()
    assert page.status_code == 200
    assert "news.example.com · eios_snippet" in html
