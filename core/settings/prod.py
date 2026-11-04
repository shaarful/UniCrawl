"""Settings for production. Used by default by wsgi.py and asgi.py.

Nothing here falls back to a development default: a missing value stops the service
at startup instead of leaving it running with an insecure key, SQLite or a lost queue.
"""
from .base import *
from .base import env

DEBUG = False
SECRET_KEY = env("DJANGO_SECRET_KEY")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
DATABASES = {"default": env.db_url("DATABASE_URL")}
# The in-memory default would silently lose scheduled crawls between processes.
CELERY_BROKER_URL = env("CELERY_BROKER_URL")

# HTTPS only. Health probes call the container directly over HTTP, so they are exempt.
SECURE_SSL_REDIRECT = True
SECURE_REDIRECT_EXEMPT = [r"^health/"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
# Browsers remember this for its whole duration, so raise it only once HTTPS is settled.
SECURE_HSTS_SECONDS = env.int("DJANGO_SECURE_HSTS_SECONDS", default=0)

# Set only behind a proxy that overwrites this header, or clients could fake a secure request.
if env.bool("DJANGO_BEHIND_HTTPS_PROXY", default=False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
