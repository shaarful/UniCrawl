"""The settings package: paths in base.py, and the rules that separate dev.py from prod.py."""
from __future__ import annotations

import importlib
import os
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import Client, override_settings

PROD_ENV = {
    "DJANGO_SECRET_KEY": "test-key",
    "DJANGO_ALLOWED_HOSTS": "example.org,localhost",
    "DATABASE_URL": "postgres://user:pass@db:5432/name",
    "CELERY_BROKER_URL": "redis://redis:6379/1",
}


def _load(monkeypatch, name: str, env: dict[str, str]):
    """Import a settings module afresh with exactly these values set."""
    for variable in (*PROD_ENV, "DJANGO_DEBUG", "DJANGO_BEHIND_HTTPS_PROXY"):
        monkeypatch.delenv(variable, raising=False)
    for variable, value in env.items():
        monkeypatch.setenv(variable, value)
    monkeypatch.delitem(sys.modules, name, raising=False)
    module = importlib.import_module(name)
    monkeypatch.delitem(sys.modules, name)
    return module


def test_base_dir_is_the_repository_root():
    from core.settings import base

    assert (base.BASE_DIR / "manage.py").is_file()
    assert all(path.is_dir() for path in base.STATICFILES_DIRS)
    assert all(path.is_dir() for path in base.TEMPLATES[0]["DIRS"])


def test_dev_falls_back_to_local_defaults(monkeypatch):
    dev = _load(monkeypatch, "core.settings.dev", {})

    assert dev.DEBUG is True
    assert dev.SECRET_KEY
    assert dev.ALLOWED_HOSTS == ["localhost", "127.0.0.1"]
    assert dev.DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3"
    assert dev.DATABASES["default"]["OPTIONS"]["timeout"] == 20


def test_prod_with_everything_set_is_locked_down(monkeypatch):
    prod = _load(monkeypatch, "core.settings.prod", {**PROD_ENV, "DJANGO_DEBUG": "True"})

    assert prod.DEBUG is False  # not even if the environment asks for it
    assert prod.ALLOWED_HOSTS == ["example.org", "localhost"]
    assert prod.DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql"
    assert prod.SECURE_SSL_REDIRECT and prod.SESSION_COOKIE_SECURE and prod.CSRF_COOKIE_SECURE
    assert not hasattr(prod, "SECURE_PROXY_SSL_HEADER")


@pytest.mark.parametrize("missing", list(PROD_ENV))
def test_prod_refuses_to_start_without_a_required_value(monkeypatch, missing):
    env = {name: value for name, value in PROD_ENV.items() if name != missing}
    with pytest.raises(ImproperlyConfigured, match=missing):
        _load(monkeypatch, "core.settings.prod", env)


def test_prod_trusts_the_proxy_header_only_when_told_to(monkeypatch):
    prod = _load(
        monkeypatch, "core.settings.prod", {**PROD_ENV, "DJANGO_BEHIND_HTTPS_PROXY": "True"},
    )
    assert prod.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # core/__init__.py imports celery.py first, so a default there would win over these.
        ("import core.wsgi", "core.settings.prod"),
        ("import core.asgi", "core.settings.prod"),
    ],
)
def test_web_entry_points_default_to_prod(code, expected):
    env = {**os.environ, **PROD_ENV}
    env.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", f"{code}; import os; print(os.environ['DJANGO_SETTINGS_MODULE'])"],
        capture_output=True, text=True, env=env, check=True,
    )
    assert result.stdout.strip().splitlines()[-1] == expected


@pytest.mark.django_db
def test_prod_https_redirect_leaves_the_health_check_alone(monkeypatch):
    prod = _load(monkeypatch, "core.settings.prod", PROD_ENV)

    with override_settings(
        SECURE_SSL_REDIRECT=prod.SECURE_SSL_REDIRECT,
        SECURE_REDIRECT_EXEMPT=prod.SECURE_REDIRECT_EXEMPT,
    ):
        client = Client()
        assert client.get("/health/").status_code == 200
        assert client.get("/admin/login/").status_code == 301
