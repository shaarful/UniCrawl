from datetime import UTC, datetime

from django.db import migrations, models


def _parse(value):
    try:
        dt = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def backfill(apps, schema_editor):
    """Fill the new fields from what existing records already hold.

    EIOS records carry the publisher and pubDate in metadata["eios"]; other
    records get the configured source's name and content type. Dates and
    bylines of HTML / RSS articles were not kept, so they stay empty until
    the article is refetched.
    """
    RawArticle = apps.get_model("raw_content", "RawArticle")
    batch = []
    rows = RawArticle.objects.select_related("source").only(
        "metadata", "source__name", "source__content_type",
    )
    for article in rows.iterator(chunk_size=500):
        eios = (article.metadata or {}).get("eios")
        if eios:
            publisher = eios.get("source") or {}
            article.published_at = _parse(eios.get("pubDate"))
            article.source_name = (publisher.get("name") or article.source.name)[:255]
            article.source_type = (publisher.get("type") or article.source.content_type)[:64]
        else:
            article.source_name = article.source.name[:255]
            article.source_type = article.source.content_type
        batch.append(article)
        if len(batch) >= 500:
            RawArticle.objects.bulk_update(batch, ["published_at", "source_name", "source_type"])
            batch = []
    RawArticle.objects.bulk_update(batch, ["published_at", "source_name", "source_type"])


class Migration(migrations.Migration):

    dependencies = [
        ('raw_content', '0003_rawarticle_metadata'),
        ('sources', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='rawarticle',
            name='author',
            field=models.CharField(blank=True, help_text='Reporter / author byline from page or feed metadata.', max_length=512),
        ),
        migrations.AddField(
            model_name='rawarticle',
            name='published_at',
            field=models.DateTimeField(blank=True, db_index=True, help_text='Publication / reporting date stated by the source (page metadata, feed entry or EIOS pubDate).', null=True),
        ),
        migrations.AddField(
            model_name='rawarticle',
            name='source_name',
            field=models.CharField(blank=True, help_text='Publisher of the article (EIOS source name, feed <source>, else the configured source name).', max_length=255),
        ),
        migrations.AddField(
            model_name='rawarticle',
            name='source_type',
            field=models.CharField(blank=True, help_text='Channel type: the source\'s content type (html, rss, social) or, for EIOS, the publisher type (webnews, wire, socialmedia-…).', max_length=64),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
