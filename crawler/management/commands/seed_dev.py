"""Dev seed command: loads the disease keywords and the WHO-EIOS board.

Usage:
    python manage.py seed_dev          # seed only
    python manage.py seed_dev --crawl  # seed + immediately crawl the seeded board
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from crawler.fetchers.eios import board_url
from disease_keywords.models import DiseaseKeyword
from sources.models import Source

# Blank language: these apply to articles in every language, and to the English
# translations EIOS supplies for articles in other languages.
_ALL_LANGUAGES = ""


def _bangla_avian_influenza(ya: str) -> str:
    return (
        "এভি" + ya + "ান ইনফ্লু" + ya
        + "েঞ্জা"
    )


_KEYWORDS_BY_DISEASE = {
    "Anthrax": (
        "anthrax", "bacillus anthracis",
        "অ্যানথ্রাক্স", "অ্যানথাক্স", "অ্যান্থ্রাক্স", "এনথ্রাক্স",
    ),
    "Rabies": (
        "rabies", "lyssavirus", "hydrophobia", "rabid dog", "rabid animal",
        "জলাতঙ্ক",
    ),
    "Avian Influenza": (
        "avian influenza", "avian flu", "bird flu", "hpai",
        "h5n1", "h5n5", "h5n6", "h5n8", "h7n9", "h9n2",
        "বার্ড ফ্লু", "এইচ৫এন১",
        # Bangla sites store this letter both ways, and the filter compares exact text.
        _bangla_avian_influenza("য়"), _bangla_avian_influenza("য়"),
    ),
}

# (disease_name, keyword, language)
KEYWORDS = [
    (disease, keyword, _ALL_LANGUAGES)
    for disease, keywords in _KEYWORDS_BY_DISEASE.items()
    for keyword in keywords
]

EIOS_BOARDS = [
    {"name": "RZDO", "board_id": "1792900211551"},
]


class Command(BaseCommand):
    help = "Seed the disease keywords and the WHO-EIOS board for local development."

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

        boards = EIOS_BOARDS
        if not settings.EIOS_API_BASE_URL:
            self.stdout.write(self.style.WARNING(
                "EIOS_API_BASE_URL is not set, so the EIOS boards were not added."
            ))
            boards = []

        src_created = 0
        source_ids = []
        for board in boards:
            obj, created = Source.objects.get_or_create(
                url=board_url(
                    settings.EIOS_API_BASE_URL, settings.EIOS_API_VERSION, board["board_id"],
                ),
                defaults={
                    "name":         board["name"],
                    "content_type": Source.ContentType.EIOS,
                    "notes":        f"WHO-EIOS board {board['board_id']} "
                                    "via the service-to-service API.",
                    # Scheduled crawling is turned on after a good test run.
                    "is_active":    False,
                    "added_by":     admin,
                },
            )
            if created:
                src_created += 1
            source_ids.append(obj.id)
            status = "created" if created else "exists "
            self.stdout.write(f"  [{status}] {obj.name}")

        self.stdout.write(self.style.SUCCESS(
            f"Sources: {src_created} created, {len(boards) - src_created} already existed."
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
