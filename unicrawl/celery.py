"""Celery application for scheduled crawl jobs.

Per-source schedules (FR-10) and retry back-off (FR-14) are configured here once
the scheduler module is implemented.
"""
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "unicrawl.settings")

app = Celery("unicrawl")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
