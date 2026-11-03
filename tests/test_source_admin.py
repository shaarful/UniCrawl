"""Adding, editing and listing sources in the admin."""
import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from raw_content.models import RawArticle
from sources.admin import _every
from sources.models import Source

ADD_URL = "admin:sources_source_add"
LIST_URL = "admin:sources_source_changelist"


def _form_data(**overrides):
    return {
        "name": "Bulletin",
        "url": "https://example.org/news",
        "content_type": "html",
        "language_hint": "",
        "notes": "",
        "crawl_interval_minutes": "30",
        "crawl_depth": "2",
        "keywords_text": "",
        **overrides,
    }


def _source(**kwargs):
    defaults = {"name": "Feed", "url": "https://example.org/feed"}
    return Source.objects.create(**{**defaults, **kwargs})


def _change_url(source):
    return reverse("admin:sources_source_change", args=[source.pk])


def _edit(client, source, **overrides):
    """Post the edit form as the page presents it, with only `overrides` changed."""
    form = client.get(_change_url(source)).context["adminform"].form
    data = {name: form[name].value() for name in form.fields}
    data = {k: "on" if v is True else ("" if v in (None, False) else v) for k, v in data.items()}
    if data.get("is_active") == "":
        del data["is_active"]
    return client.post(_change_url(source), {**data, **overrides})


@pytest.mark.django_db
def test_add_page_only_asks_for_what_a_new_source_needs(admin_client):
    content = admin_client.get(reverse(ADD_URL)).content.decode()

    assert "Crawl every (minutes)" in content
    assert "Keywords for this source only" in content
    assert 'name="is_active"' not in content
    assert "Back-off" not in content
    assert "Last crawled at" not in content
    assert 'name="added_by"' not in content
    assert "IEDCR" not in content


@pytest.mark.django_db
def test_add_stores_seconds_and_keywords_and_starts_unscheduled(admin_client, admin_user):
    response = admin_client.post(reverse(ADD_URL), _form_data(
        keywords_text="anthrax\n\n  nipah virus \nanthrax\n",
    ))

    source = Source.objects.get()
    assert source.crawl_interval_seconds == 1800
    assert source.crawl_depth == 2
    assert source.keyword_filter_overrides == ["anthrax", "nipah virus"]
    assert source.is_active is False
    assert source.added_by == admin_user
    assert response.status_code == 302
    assert response["Location"] == _change_url(source)


@pytest.mark.django_db
def test_add_with_no_keywords_uses_the_shared_list(admin_client):
    admin_client.post(reverse(ADD_URL), _form_data())

    assert Source.objects.get().keyword_filter_overrides is None


@pytest.mark.django_db
def test_add_ignores_a_posted_is_active(admin_client):
    admin_client.post(reverse(ADD_URL), _form_data(is_active="on"))

    assert Source.objects.get().is_active is False


@pytest.mark.django_db
@pytest.mark.parametrize("field,value", [
    ("crawl_interval_minutes", "0"),
    ("crawl_depth", "9"),
    ("url", "not a url"),
])
def test_add_rejects_bad_values(admin_client, field, value):
    response = admin_client.post(reverse(ADD_URL), _form_data(**{field: value}))

    assert response.status_code == 200
    assert field in response.context["adminform"].form.errors
    assert not Source.objects.exists()


@pytest.mark.django_db
def test_save_and_add_another_still_goes_to_the_add_page(admin_client):
    response = admin_client.post(reverse(ADD_URL), _form_data(_addanother="1"))

    assert response["Location"] == reverse(ADD_URL)


@pytest.mark.django_db
def test_edit_changes_interval_and_keywords(admin_client):
    source = _source(crawl_interval_seconds=3600)

    response = _edit(
        admin_client, source, crawl_interval_minutes="15", keywords_text="dengue", is_active="on",
    )

    assert response.status_code == 302
    source.refresh_from_db()
    assert source.crawl_interval_seconds == 900
    assert source.keyword_filter_overrides == ["dengue"]
    assert source.is_active is True


@pytest.mark.django_db
def test_edit_elsewhere_keeps_an_interval_that_is_not_whole_minutes(admin_client):
    source = _source(crawl_interval_seconds=90)

    _edit(admin_client, source, notes="Checked")

    source.refresh_from_db()
    assert source.notes == "Checked"
    assert source.crawl_interval_seconds == 90


@pytest.mark.django_db
def test_edit_elsewhere_keeps_an_empty_keyword_list(admin_client):
    # An empty list matches nothing, which differs from null (the shared list).
    source = _source(keyword_filter_overrides=[])

    _edit(admin_client, source, notes="Checked")

    source.refresh_from_db()
    assert source.keyword_filter_overrides == []


@pytest.mark.django_db
def test_clearing_keywords_returns_to_the_shared_list(admin_client):
    source = _source(keyword_filter_overrides=["anthrax"])

    _edit(admin_client, source, keywords_text="")

    source.refresh_from_db()
    assert source.keyword_filter_overrides is None


@pytest.mark.django_db
def test_edit_keeps_a_depth_outside_the_usual_choices(admin_client):
    source = _source(crawl_depth=5)

    response = _edit(admin_client, source, notes="Checked")

    assert response.status_code == 302
    source.refresh_from_db()
    assert source.crawl_depth == 5


@pytest.mark.django_db
def test_change_page_has_run_articles_and_open_site(admin_client):
    source = _source()

    content = admin_client.get(_change_url(source)).content.decode()

    assert f'action="{reverse("crawler:start", args=[source.pk])}"' in content
    assert f"?source__id__exact={source.pk}" in content
    assert "Open site" in content
    assert 'name="is_active"' in content
    assert "Currently:" not in content


@pytest.mark.django_db
def test_change_page_of_an_eios_board_does_not_open_its_api_url(admin_client):
    source = _source(content_type=Source.ContentType.EIOS)

    assert "Open site" not in admin_client.get(_change_url(source)).content.decode()


@pytest.mark.django_db
def test_list_shows_status_schedule_articles_and_run_button(admin_client):
    source = _source(is_active=True, crawl_interval_seconds=7200)
    RawArticle.objects.create(
        source=source, url="https://example.org/a", http_status=200, fetched_at=timezone.now(),
        title="T", body="B", url_fingerprint="a", content_hash="a",
    )

    response = admin_client.get(reverse(LIST_URL))

    content = response.content.decode()
    assert response.context["title"] == "Sources"
    assert "Scheduled" in content
    assert "Every 2 hours" in content
    assert "News / HTML" in content
    assert "Never" in content
    assert f'?source__id__exact={source.pk}">1</a>' in content
    assert f'formaction="{reverse("crawler:start", args=[source.pk])}"' in content


@pytest.mark.django_db
@pytest.mark.parametrize("fields,label", [
    ({"is_temporarily_unavailable": True, "is_active": True}, "Unavailable"),
    ({"consecutive_failures": 1, "is_active": True}, "1 failed try"),
    ({"consecutive_failures": 2}, "2 failed tries"),
    ({"is_active": True}, "Scheduled"),
    ({}, "Not scheduled"),
])
def test_list_status_label(admin_client, fields, label):
    _source(**fields)

    assert f">{label}</span>" in admin_client.get(reverse(LIST_URL)).content.decode()


@pytest.mark.parametrize("seconds,label", [
    (60, "Every minute"),
    (90, "Every 2 minutes"),
    (900, "Every 15 minutes"),
    (3600, "Every hour"),
    (7200, "Every 2 hours"),
    (86400, "Every day"),
    (172800, "Every 2 days"),
])
def test_schedule_wording(seconds, label):
    assert _every(seconds) == label


@pytest.mark.django_db
def test_run_button_on_the_list_starts_a_crawl(admin_client, monkeypatch):
    source = _source()
    started = []

    class Run:
        pk = 1

    def fake_start(source_id, *, user=None):
        started.append(source_id)
        return Run

    monkeypatch.setattr("crawler.views.start_run", fake_start)

    # The button submits the whole list form, so its other fields come along.
    response = admin_client.post(
        reverse("crawler:start", args=[source.pk]),
        {"action": "", "select_across": "0", "index": "0"},
    )

    assert started == [source.pk]
    assert response.status_code == 302


@pytest.mark.django_db
def test_bulk_actions_start_and_stop_scheduling(admin_client):
    source = _source()
    url = reverse(LIST_URL)

    admin_client.post(url, {"action": "start_scheduling", "_selected_action": [source.pk]})
    source.refresh_from_db()
    assert source.is_active is True

    admin_client.post(url, {"action": "stop_scheduling", "_selected_action": [source.pk]})
    source.refresh_from_db()
    assert source.is_active is False


@pytest.mark.django_db
def test_view_only_user_can_open_list_and_source(client, django_user_model):
    source = _source(keyword_filter_overrides=["anthrax"], crawl_interval_seconds=1800)
    user = django_user_model.objects.create_user("viewer", password="x", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="view_source"))
    client.force_login(user)

    assert client.get(reverse(LIST_URL)).status_code == 200
    response = client.get(_change_url(source))

    assert response.status_code == 200
    content = response.content.decode()
    assert "anthrax" in content
    assert "30" in content
    assert 'name="keywords_text"' not in content
