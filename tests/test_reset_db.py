"""The reset_db command: asks first, then leaves only a freshly migrated database."""
from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from disease_keywords.models import DiseaseKeyword


def _reset(answer: str) -> None:
    with patch("builtins.input", return_value=answer):
        call_command("reset_db", stdout=StringIO())


def _add_keyword() -> None:
    DiseaseKeyword.objects.create(disease_name="Rabies", keyword="rabies")


@pytest.mark.django_db
def test_wrong_name_deletes_nothing():
    _add_keyword()

    with pytest.raises(CommandError, match="Nothing was deleted"):
        _reset("wrong-name")

    assert DiseaseKeyword.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_right_name_deletes_everything_and_migrates():
    _add_keyword()
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE left_over_table (id integer)")

    _reset(str(connection.settings_dict["NAME"]))

    assert DiseaseKeyword.objects.count() == 0
    assert "left_over_table" not in connection.introspection.table_names()
    assert MigrationRecorder(connection).applied_migrations()
