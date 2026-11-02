"""Django settings for the UniCrawl service.

Skeleton settings — values are environment-driven via django-environ.
See SRS RZDO_EWS_SRS_v1.5 sections 2.4 (Operating Environment) and 4 (NFRs).
"""
from __future__ import annotations

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CRAWLER_DEFAULT_INTERVAL_SECONDS=(int, 3600),
    CRAWLER_RETRY_BACKOFF_MINUTES=(list, [1, 4, 16]),
    CRAWLER_SEMANTIC_DEDUP_THRESHOLD=(float, 0.85),
    CRAWLER_SEMANTIC_DEDUP_ENABLED=(bool, False),
    CRAWLER_SEMANTIC_MODEL=(str, "paraphrase-multilingual-MiniLM-L12-v2"),
    CRAWLER_SEMANTIC_MAX_CANDIDATES=(int, 10_000),
)
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY", default="dev-insecure-key-change-me")
DEBUG = env("DJANGO_DEBUG")
ALLOWED_HOSTS = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "sources.apps.SourcesConfig",
    "raw_content.apps.RawContentConfig",
    "crawler.apps.CrawlerConfig",
    "health.apps.HealthConfig",
    "disease_keywords.apps.DiseaseKeywordsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "unicrawl.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "unicrawl.wsgi.application"
ASGI_APPLICATION = "unicrawl.asgi.application"

# SRS §6.1: PostgreSQL 18+ with PostGIS. Falls back to sqlite for skeleton bring-up.
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

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": env.cache_url("REDIS_URL", default="locmemcache://"),
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="memory://")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="cache+memory://")
CELERY_TASK_DEFAULT_QUEUE = "unicrawl.default"

# Beat: poll for due sources every 30 s (FR-10)
CELERY_BEAT_SCHEDULE = {
    "crawler-tick": {
        "task": "crawler.tasks.tick",
        "schedule": 30.0,
    },
}

# Crawler tunables (FR-10, FR-11/23, FR-14)
CRAWLER_DEFAULT_INTERVAL_SECONDS = env("CRAWLER_DEFAULT_INTERVAL_SECONDS")
CRAWLER_RETRY_BACKOFF_MINUTES = [int(x) for x in env("CRAWLER_RETRY_BACKOFF_MINUTES")]
CRAWLER_SEMANTIC_DEDUP_THRESHOLD = env("CRAWLER_SEMANTIC_DEDUP_THRESHOLD")
# Semantic dedup is opt-in: the model (~420 MB) is downloaded on first use.
# Enable only after running: pipenv install sentence-transformers
CRAWLER_SEMANTIC_DEDUP_ENABLED = env("CRAWLER_SEMANTIC_DEDUP_ENABLED")
CRAWLER_SEMANTIC_MODEL = env("CRAWLER_SEMANTIC_MODEL")
CRAWLER_SEMANTIC_MAX_CANDIDATES = env("CRAWLER_SEMANTIC_MAX_CANDIDATES")

LLM_QUEUE_NAME = env("LLM_QUEUE_NAME", default="unicrawl.matched_articles")

EIOS_API_BASE_URL = env("EIOS_API_BASE_URL", default="")
EIOS_API_VERSION = env("EIOS_API_VERSION", default="1.0")
EIOS_TOKEN_URL = env("EIOS_TOKEN_URL", default="")
EIOS_CLIENT_ID = env("EIOS_CLIENT_ID", default="")
EIOS_CLIENT_SECRET = env("EIOS_CLIENT_SECRET", default="")
EIOS_SCOPE = env("EIOS_SCOPE", default="")
EIOS_BACKFILL_HOURS = env.int("EIOS_BACKFILL_HOURS", default=24)
EIOS_WINDOW_MINUTES = env.int("EIOS_WINDOW_MINUTES", default=60)
EIOS_PAGE_SIZE = env.int("EIOS_PAGE_SIZE", default=100)
EIOS_LINK_FETCH_WORKERS = env.int("EIOS_LINK_FETCH_WORKERS", default=16)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
}

# Structured JSON logs (NFR §4.2.2). Replace with structlog config when wiring up.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
