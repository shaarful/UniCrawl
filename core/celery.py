"""Celery application for scheduled crawl jobs.

Per-source schedules and retry back-off are configured here once
the scheduler module is implemented.
"""
from __future__ import annotations

from celery import Celery

# DJANGO_SETTINGS_MODULE must come from the environment. A default here would run whenever
# the core package is imported and override the production default in wsgi.py and asgi.py.
app = Celery("core")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
