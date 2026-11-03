"""Adding WHO-EIOS boards as sources from the crawl dashboard."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from django.urls import reverse

from crawler.fetchers.eios import EIOSFetcher, board_url
from sources.models import Source

API = "https://eios.example.org/portal/api"
ADD_URL = reverse("crawler:eios-board-add")


@pytest.fixture(autouse=True)
def _eios_settings(settings):
    settings.EIOS_API_BASE_URL = API
    settings.EIOS_API_VERSION = "1.0"
    settings.EIOS_CLIENT_ID = "cid"
    settings.EIOS_TOKEN_URL = "https://login.example.com/token"


@pytest.fixture(autouse=True)
def board_check():
    """No test here reaches EIOS. By default the board exists."""
    with patch.object(EIOSFetcher, "board_article_count", return_value=120) as check:
        yield check


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
def test_form_explains_which_box_is_which(admin_client):
    html = admin_client.get(reverse("crawler:dashboard")).content.decode()

    assert "usually a long number, not its name" in html
    assert "Your own label for this board" in html


@pytest.mark.django_db
def test_board_is_checked_with_eios_before_saving(admin_client, board_check):
    response = _post(admin_client, board="1234")

    assert response.status_code == 302
    assert board_check.call_args.args == (board_url(API, "1.0", "1234"),)
    assert Source.objects.filter(url=board_url(API, "1.0", "1234")).exists()


@pytest.mark.django_db
def test_board_eios_does_not_know_is_rejected(admin_client, board_check):
    board_check.return_value = None

    response = _post(admin_client, board="12345", name="My board")

    content = response.content.decode()
    assert response.status_code == 400
    assert "EIOS has no board with the ID “12345”" in content
    assert "not the board&#x27;s name" in content or "not the board's name" in content
    assert not Source.objects.exists()


@pytest.mark.django_db
def test_swapped_board_id_and_name_are_pointed_out(admin_client, board_check):
    board_check.return_value = None

    response = _post(admin_client, board="RZDO", name="1792900211551")

    content = response.content.decode()
    assert response.status_code == 400
    assert "try “1792900211551” as the board ID and “RZDO” as the name" in content
    assert not Source.objects.exists()


@pytest.mark.django_db
def test_checking_a_duplicate_does_not_call_eios(admin_client, board_check):
    _post(admin_client, board="1234")
    board_check.reset_mock()

    _post(admin_client, board="1234", name="Other name")

    board_check.assert_not_called()


@pytest.mark.django_db
def test_message_says_how_big_the_first_crawl_is(admin_client, settings):
    settings.EIOS_BACKFILL_HOURS = 24

    response = admin_client.post(ADD_URL, {"interval_minutes": 60, "board": "1234"}, follow=True)

    assert "EIOS lists 120 articles from the last 24 hours" in response.content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("error", [
    httpx.ConnectTimeout("slow"),
    httpx.HTTPStatusError(
        "forbidden", request=httpx.Request("GET", "https://x"), response=httpx.Response(403),
    ),
    RuntimeError("EIOS credentials missing"),
    ValueError("not JSON"),
])
def test_board_is_saved_with_a_warning_when_eios_cannot_answer(admin_client, board_check, error):
    board_check.side_effect = error

    response = admin_client.post(ADD_URL, {"interval_minutes": 60, "board": "1234"}, follow=True)

    assert Source.objects.filter(url=board_url(API, "1.0", "1234")).exists()
    content = response.content.decode()
    assert "Could not check this board with EIOS" in content
    assert "credentials missing" not in content  # the reason is the error type only


@pytest.mark.django_db
def test_add_board_requires_staff(client):
    response = _post(client, board="1234")
    assert response.status_code == 302
    assert "/admin/login/" in response["Location"]
    assert not Source.objects.exists()
