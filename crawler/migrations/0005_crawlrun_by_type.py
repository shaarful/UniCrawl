from django.db import migrations


def clear_site_counts(apps, schema_editor):
    """These held individual sites, which would read as types of site after the rename."""
    apps.get_model("crawler", "CrawlRun").objects.update(skipped_by_type={}, errors_by_type={})


class Migration(migrations.Migration):

    dependencies = [
        ("crawler", "0004_crawlrun_by_site"),
    ]

    operations = [
        migrations.RenameField("crawlrun", "skipped_by_site", "skipped_by_type"),
        migrations.RenameField("crawlrun", "errors_by_site", "errors_by_type"),
        migrations.RunPython(clear_site_counts, migrations.RunPython.noop),
    ]
