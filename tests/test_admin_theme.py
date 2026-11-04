"""The RZDO EWS look of the Django admin."""
import pytest
from django.contrib.staticfiles import finders
from django.urls import reverse


def test_theme_stylesheet_can_be_found():
    assert finders.find("core/admin_theme.css")


@pytest.mark.django_db
def test_admin_pages_use_the_theme_and_brand(admin_client):
    for url in (
        reverse("admin:index"),
        reverse("admin:sources_source_changelist"),
        reverse("admin:sources_source_add"),
    ):
        response = admin_client.get(url)

        assert response.status_code == 200
        content = response.content.decode()
        assert "core/admin_theme.css" in content
        assert "RZDO EWS" in content
        assert "Django administration" not in content


@pytest.mark.django_db
def test_header_links_to_the_crawl_dashboard_and_home_has_readable_app_names(admin_client):
    content = admin_client.get(reverse("admin:index")).content.decode()

    assert f'href="{reverse("crawler:dashboard")}"' in content
    assert "View site" not in content
    assert "Raw content" in content
    assert "Disease keywords" in content
    assert ">LLM processing</a>" in content
    assert "LLM extraction" not in content
    assert "Raw_Content" not in content
    assert "Disease_Keywords" not in content
