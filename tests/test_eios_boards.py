"""Adding WHO-EIOS boards as sources from the crawl dashboard."""
from __future__ import annotations

import pytest
from django.urls import reverse

from crawler.fetchers.eios import board_url
from sources.models import Source

API = "https://eios.example.org/portal/api"
ADD_URL = reverse("crawler:eios-board-add")


@pytest.fixture(autouse=True)
def _eios_settings(settings):
    settings.EIOS_API_BASE_URL = API
    settings.EIOS_API_VERSION = "1.0"
    settings.EIOS_CLIENT_ID = "cid"
    settings.EIOS_TOKEN_URL = "https://login.example.com/token"


def _post(client, **data):
    return client.post(ADD_URL, {"interval_minutes": 60, **data})


@pytest.mark.django_db
def test_add_board_by_id(admin_client):
    response = _post(admin_client, board="1234")

    assert response.status_code == 302
    assert response["Location"] == reverse("crawler:dashboard")
    source = Source.objects.get()
    assert source.url == board_url(API, "1.0", "1234")
    assert source.name == "WHO-EIOS board 1234"
    assert source.content_type == Source.ContentType.EIOS
    assert source.crawl_interval_seconds == 3600
    assert source.is_active is False
    assert source.added_by.is_superuser


@pytest.mark.django_db
def test_add_board_by_url_with_name_and_activation(admin_client):
    other_api = "https://eios.other.org/api"
    _post(admin_client, board=f"{board_url(other_api, '2.0', 'abc-9')}/?x=1#top",
          name="Regional board", interval_minutes=15, is_active="on")

    source = Source.objects.get()
    assert source.url == board_url(other_api, "2.0", "abc-9")
    assert source.name == "Regional board"
    assert source.crawl_interval_seconds == 900
    assert source.is_active is True


@pytest.mark.django_db
def test_duplicate_board_is_rejected(admin_client):
    _post(admin_client, board="1234")
    response = _post(admin_client, board=board_url(API, "1.0", "1234"), name="Other name")

    assert response.status_code == 400
    assert "already a source" in response.content.decode()
    assert Source.objects.count() == 1


@pytest.mark.django_db
def test_duplicate_name_is_rejected(admin_client):
    _post(admin_client, board="1", name="Board")
    response = _post(admin_client, board="2", name="Board")

    assert response.status_code == 400
    assert Source.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("board", ["12 34", "https://news.example.com/feed", ""])
def test_invalid_board_is_rejected(admin_client, board):
    response = _post(admin_client, board=board)
    assert response.status_code == 400
    assert not Source.objects.exists()


@pytest.mark.django_db
def test_board_id_needs_configured_api_base(admin_client, settings):
    settings.EIOS_API_BASE_URL = ""
    response = _post(admin_client, board="1234")
    assert response.status_code == 400
    assert "EIOS_API_BASE_URL" in response.content.decode()


@pytest.mark.django_db
def test_dashboard_shows_form_and_credentials_warning(admin_client, settings):
    html = admin_client.get(reverse("crawler:dashboard")).content.decode()
    assert "Add EIOS board" in html and "not set" not in html

    settings.EIOS_CLIENT_ID = ""
    html = admin_client.get(reverse("crawler:dashboard")).content.decode()
    assert "credentials" in html


@pytest.mark.django_db
def test_add_board_requires_staff(client):
    response = _post(client, board="1234")
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]
    assert not Source.objects.exists()
