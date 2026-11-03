"""The sign-in page."""
import pytest

LOGIN_URL = "/admin/login/"
NEXT_URL = "/crawl/"


@pytest.fixture
def staff(django_user_model):
    return django_user_model.objects.create_user("ana", password="secret-pw", is_staff=True)


def test_page_shows_the_form_without_the_site_navigation(client):
    response = client.get(LOGIN_URL)

    assert response.status_code == 200
    assert "crawler/login.html" in [t.name for t in response.templates]
    content = response.content.decode()
    assert "<title>Sign in · RZDO EWS</title>" in content
    assert "UniCrawl" not in content
    assert 'name="username"' in content
    assert 'name="password"' in content
    assert 'type="password"' in content
    assert 'aria-label="Show password"' in content
    assert "csrfmiddlewaretoken" in content
    assert "<nav>" not in content


@pytest.mark.django_db
def test_correct_password_goes_to_the_requested_page(client, staff):
    response = client.post(
        f"{LOGIN_URL}?next={NEXT_URL}",
        {"username": "ana", "password": "secret-pw", "next": NEXT_URL},
    )

    assert response.status_code == 302
    assert response["Location"] == NEXT_URL


@pytest.mark.django_db
def test_wrong_password_shows_an_error_and_keeps_the_username(client, staff):
    response = client.post(LOGIN_URL, {"username": "ana", "password": "wrong"})

    assert response.status_code == 200
    content = response.content.decode()
    assert "login-error" in content
    assert "Error: Sign in" in content
    assert 'value="ana"' in content


@pytest.mark.django_db
def test_signed_in_user_without_staff_access_is_told_why(client, django_user_model):
    user = django_user_model.objects.create_user("ben", password="secret-pw")
    client.force_login(user)

    content = client.get(LOGIN_URL).content.decode()

    assert "You are signed in as ben" in content
