"""Delete every table in the configured database, then rebuild them with migrate.

Usage:
    python manage.py reset_db
"""
from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


def _extension_tables(cursor) -> set[str]:
    """Tables owned by a PostgreSQL extension, such as PostGIS's spatial_ref_sys."""
    cursor.execute(
        "SELECT c.relname FROM pg_depend d JOIN pg_class c ON c.oid = d.objid "
        "WHERE d.classid = 'pg_class'::regclass AND d.deptype = 'e'"
    )
    return {row[0] for row in cursor.fetchall()}


def _drop_all_tables() -> int:
    is_postgres = connection.vendor == "postgresql"
    # SQLite cannot switch off foreign key checks inside a transaction.
    with connection.constraint_checks_disabled(), transaction.atomic():
        with connection.cursor() as cursor:
            tables = set(connection.introspection.table_names(cursor))
            if is_postgres:
                tables -= _extension_tables(cursor)
            for table in tables:
                cascade = " CASCADE" if is_postgres else ""
                cursor.execute(f"DROP TABLE {connection.ops.quote_name(table)}{cascade}")
    return len(tables)


class Command(BaseCommand):
    help = "Permanently delete all data in the configured database and run migrate."

    def handle(self, *args, **options):
        name = str(connection.settings_dict["NAME"])
        host = connection.settings_dict.get("HOST") or "local"
        self.stdout.write(self.style.WARNING(
            f"This permanently deletes ALL data in database '{name}' on {host}."
        ))
        if input("Type the database name to continue: ") != name:
            raise CommandError("Name did not match. Nothing was deleted.")

        dropped = _drop_all_tables()
        self.stdout.write(self.style.WARNING(f"Dropped {dropped} tables."))
        call_command(
            "migrate", interactive=False, verbosity=options["verbosity"], stdout=self.stdout,
        )
        self.stdout.write(self.style.SUCCESS("Database reset."))
