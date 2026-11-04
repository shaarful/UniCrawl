"""Shared test setup."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _queue_on_by_default(settings):
    """Tests get the default, whatever LLM_QUEUE_ENABLED says in a developer's own .env."""
    settings.LLM_QUEUE_ENABLED = True
