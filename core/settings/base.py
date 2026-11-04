"""Settings shared by every environment. dev.py and prod.py extend this file."""
from __future__ import annotations

from pathlib import Path

import environ

# core/settings/base.py -> the repository root
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    CRAWLER_DEFAULT_INTERVAL_SECONDS=(int, 3600),
    CRAWLER_RETRY_BACKOFF_MINUTES=(list, [1, 4, 16]),
    CRAWLER_SEMANTIC_DEDUP_THRESHOLD=(float, 0.85),
    CRAWLER_SEMANTIC_DEDUP_ENABLED=(bool, False),
    CRAWLER_SEMANTIC_MODEL=(str, "paraphrase-multilingual-MiniLM-L12-v2"),
    CRAWLER_SEMANTIC_MAX_CANDIDATES=(int, 10_000),
)
environ.Env.read_env(BASE_DIR / ".env")

CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

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
    "llm.apps.LlmConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Serves the admin and dashboard static files under gunicorn, where DEBUG is off.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Searched before the apps, so core/templates can override the stock admin.
        "DIRS": [BASE_DIR / "core" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            # core is not an installed app, so its tag library is named here.
            "libraries": {"workflow": "core.templatetags.workflow"},
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": env.cache_url("REDIS_URL", default="locmemcache://"),
}

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="memory://")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="cache+memory://")
CELERY_TASK_DEFAULT_QUEUE = "rzdo.default"

# Beat: poll for due sources every 30 s
CELERY_BEAT_SCHEDULE = {
    "crawler-tick": {
        "task": "crawler.tasks.tick",
        "schedule": 30.0,
    },
}

# Crawler tunables
CRAWLER_DEFAULT_INTERVAL_SECONDS = env("CRAWLER_DEFAULT_INTERVAL_SECONDS")
CRAWLER_RETRY_BACKOFF_MINUTES = [int(x) for x in env("CRAWLER_RETRY_BACKOFF_MINUTES")]
CRAWLER_SEMANTIC_DEDUP_THRESHOLD = env("CRAWLER_SEMANTIC_DEDUP_THRESHOLD")
# Semantic dedup is opt-in: the model (~420 MB) is downloaded on first use.
# Enable only after running: pipenv install sentence-transformers
CRAWLER_SEMANTIC_DEDUP_ENABLED = env("CRAWLER_SEMANTIC_DEDUP_ENABLED")
CRAWLER_SEMANTIC_MODEL = env("CRAWLER_SEMANTIC_MODEL")
CRAWLER_SEMANTIC_MAX_CANDIDATES = env("CRAWLER_SEMANTIC_MAX_CANDIDATES")
# Headless browser for pages that are empty until JavaScript runs.
CRAWLER_BROWSER_ENABLED = env.bool("CRAWLER_BROWSER_ENABLED", default=True)
CRAWLER_BROWSER_TIMEOUT_SECONDS = env.int("CRAWLER_BROWSER_TIMEOUT_SECONDS", default=30)
# Following links from list pages (crawl depth 2+).
CRAWLER_MAX_PAGES_PER_RUN = env.int("CRAWLER_MAX_PAGES_PER_RUN", default=50)
CRAWLER_LINK_DELAY_SECONDS = env.float("CRAWLER_LINK_DELAY_SECONDS", default=1.0)

# False: crawls leave matched articles as "matched" and do not contact Redis.
LLM_QUEUE_ENABLED = env.bool("LLM_QUEUE_ENABLED", default=True)
LLM_QUEUE_NAME = env("LLM_QUEUE_NAME", default="rzdo.matched_articles")
LLM_DEAD_LETTER_QUEUE_NAME = env("LLM_DEAD_LETTER_QUEUE_NAME", default=f"{LLM_QUEUE_NAME}.dead")
LLM_CONSUMER_GROUP = env("LLM_CONSUMER_GROUP", default="llm-extraction")
LLM_MODEL = env("LLM_MODEL", default="gemini-3.5-flash-lite")
LLM_GCP_LOCATION = env("LLM_GCP_LOCATION", default="global")
RZDO_GCP_PROJECT = env("RZDO_GCP_PROJECT", default="")
RZDO_GCP_CREDENTIALS = env("RZDO_GCP_CREDENTIALS", default="")
LLM_MAX_ATTEMPTS = env.int("LLM_MAX_ATTEMPTS", default=3)
LLM_MAX_INPUT_CHARS = env.int("LLM_MAX_INPUT_CHARS", default=50_000)
LLM_REQUEST_TIMEOUT_SECONDS = env.int("LLM_REQUEST_TIMEOUT_SECONDS", default=300)
# Must stay above the request timeout, or a message still in progress can be reclaimed.
LLM_CLAIM_IDLE_SECONDS = env.int("LLM_CLAIM_IDLE_SECONDS", default=600)

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
STATICFILES_DIRS = [BASE_DIR / "core" / "static"]

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
