"""Dev seed command — loads disease keywords and test sources, then test-crawls one.

Usage:
    python manage.py seed_dev          # seed only
    python manage.py seed_dev --crawl  # seed + immediately crawl the first source
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from disease_keywords.models import DiseaseKeyword
from sources.models import Source

KEYWORDS = [
    # (disease_name, keyword, language)
    ("Dengue",          "dengue",           ""),
    ("Dengue",          "dengue fever",     ""),
    ("Dengue",          "breakbone fever",  ""),
    ("Cholera",         "cholera",          ""),
    ("Cholera",         "vibrio cholerae",  ""),
    ("Mpox",            "mpox",             ""),
    ("Mpox",            "monkeypox",        ""),
    ("Avian Influenza", "bird flu",         ""),
    ("Avian Influenza", "avian influenza",  ""),
    ("Avian Influenza", "h5n1",             ""),
    ("Ebola",           "ebola",            ""),
    ("Marburg",         "marburg",          ""),
    ("Rabies",          "rabies",           ""),
    ("Anthrax",         "anthrax",          ""),
    ("Plague",          "plague",           ""),
    ("Plague",          "yersinia pestis",  ""),
]

SOURCES = [
    {
        "name":          "Outbreak News Today",
        "url":           "https://outbreaknewstoday.com/feed/",
        "content_type":  Source.ContentType.RSS,
        "language_hint": "en",
        "notes":         "Outbreak News Today — independent disease surveillance blog.",
    },
    {
        "name":          "ReliefWeb Health Alerts",
        "url":           "https://reliefweb.int/updates/rss.xml?primary_country=0&source=0&theme=3&content-format=Report",
        "content_type":  Source.ContentType.RSS,
        "language_hint": "en",
        "notes":         "ReliefWeb humanitarian health reports feed (theme=Health).",
    },
    {
        "name":          "WHO Disease Outbreak News",
        "url":           "https://www.who.int/emergencies/disease-outbreak-news",
        "content_type":  Source.ContentType.HTML,
        "language_hint": "en",
        "notes":         "WHO Disease Outbreak News landing page (HTML source).",
    },
]


def _eios_source() -> dict | None:
    """The configured EIOS board as a source, or None if EIOS is not configured."""
    from django.conf import settings

    from crawler.fetchers.eios import board_url

    if not (settings.EIOS_BOARD_ID and settings.EIOS_API_BASE_URL and settings.EIOS_CLIENT_ID):
        return None
    return {
        "name":          f"WHO-EIOS board {settings.EIOS_BOARD_ID}",
        "url":           board_url(settings.EIOS_API_BASE_URL, settings.EIOS_API_VERSION,
                                   settings.EIOS_BOARD_ID),
        "content_type":  Source.ContentType.EIOS,
        "language_hint": "",
        "notes":         "WHO-EIOS board articles via the service-to-service API (FR-07).",
    }


class Command(BaseCommand):
    help = "Seed disease keywords and test sources for local development."

    def add_arguments(self, parser):
        parser.add_argument(
            "--crawl",
            action="store_true",
            help="After seeding, immediately crawl all sources.",
        )
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete all existing keywords, sources, and raw articles before seeding.",
        )

    def handle(self, *args, **options):
        if options["reset"]:
            from raw_content.models import RawArticle
            deleted_articles, _ = RawArticle.objects.all().delete()
            deleted_sources, _  = Source.objects.all().delete()
            deleted_kw, _       = DiseaseKeyword.objects.all().delete()
            self.stdout.write(self.style.WARNING(
                f"Reset: deleted {deleted_articles} articles, "
                f"{deleted_sources} sources, {deleted_kw} keywords."
            ))

        # --- keywords ---
        kw_created = 0
        for disease_name, keyword, language in KEYWORDS:
            _, created = DiseaseKeyword.objects.get_or_create(
                keyword=keyword,
                language=language,
                defaults={"disease_name": disease_name, "is_active": True},
            )
            if created:
                kw_created += 1
        self.stdout.write(self.style.SUCCESS(
            f"Keywords: {kw_created} created, {len(KEYWORDS) - kw_created} already existed."
        ))

        # --- sources ---
        User = get_user_model()
        admin = User.objects.filter(is_superuser=True).first()

        sources = [*SOURCES, *filter(None, [_eios_source()])]
        src_created = 0
        source_ids = []
        for s in sources:
            obj, created = Source.objects.get_or_create(
                url=s["url"],
                defaults={
                    "name":          s["name"],
                    "content_type":  s["content_type"],
                    "language_hint": s["language_hint"],
                    "notes":         s["notes"],
                    "is_active":     True,
                    "added_by":      admin,
                },
            )
            if created:
                src_created += 1
            source_ids.append(obj.id)
            status = "created" if created else "exists "
            self.stdout.write(f"  [{status}] {obj.name}")

        self.stdout.write(self.style.SUCCESS(
            f"Sources: {src_created} created, {len(sources) - src_created} already existed."
        ))

        # --- optional crawl ---
        if options["crawl"]:
            self.stdout.write("\nCrawling sources now...")
            from crawler.pipeline import run_for_source
            from raw_content.models import RawArticle

            for source_id in source_ids:
                source = Source.objects.get(pk=source_id)
                self.stdout.write(f"\n  Crawling: {source.name} ...")
                try:
                    run_for_source(source_id)
                    count   = RawArticle.objects.filter(source=source).count()
                    matched = RawArticle.objects.filter(source=source, is_keyword_matched=True).count()
                    self.stdout.write(self.style.SUCCESS(
                        f"    OK — {count} articles stored, {matched} keyword-matched."
                    ))
                except Exception as exc:
                    self.stdout.write(self.style.ERROR(f"    FAILED: {exc}"))

            self.stdout.write("\nDone. Check results:")
            self.stdout.write("  http://127.0.0.1:8000/admin/raw_content/rawarticle/")
