"""Settings for local development and the dev server. Used by default by manage.py and Celery."""
from .base import *
from .base import BASE_DIR, env

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-key-change-me")
DEBUG = env.bool("DJANGO_DEBUG", default=True)
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

# SRS §6.1 asks for PostgreSQL 18+ with PostGIS; SQLite is the fallback for local work.
DATABASES = {
    "default": env.db_url(
        "DATABASE_URL",
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    ),
}
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    # The crawl dashboard runs crawls in a background thread while pages poll
    # progress: WAL lets reads proceed during writes, and the timeout waits out
    # brief write locks instead of failing with "database is locked".
    DATABASES["default"].setdefault("OPTIONS", {}).update({
        "timeout": 20,
        "init_command": "PRAGMA journal_mode=WAL;",
    })
