"""The seed_dev command: the disease keywords and the WHO-EIOS board."""
from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from crawler.fetchers.eios import board_url, parse_board_url
from crawler.filters.keyword import matches_active_diseases
from crawler.management.commands.seed_dev import KEYWORDS
from disease_keywords.models import DiseaseKeyword
from sources.models import Source

API = "https://eios.example.org/portal/api"
BOARD_ID = "1792900211551"


@pytest.fixture(autouse=True)
def _eios_settings(settings):
    settings.EIOS_API_BASE_URL = API
    settings.EIOS_API_VERSION = "1.0"


def _seed(*args) -> str:
    out = StringIO()
    call_command("seed_dev", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_seeds_only_the_three_diseases_for_every_language():
    _seed()

    assert set(DiseaseKeyword.objects.values_list("disease_name", flat=True)) == {
        "Anthrax", "Rabies", "Avian Influenza",
    }
    assert DiseaseKeyword.objects.count() == len(KEYWORDS) == 26
    assert not DiseaseKeyword.objects.exclude(language="").exists()
    assert not DiseaseKeyword.objects.filter(is_active=False).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("text,language,expected", [
    ("Rabies death after a rabid dog bite", "en", ["Rabies"]),
    ("Un cas de rabies signalé", "fr", ["Rabies"]),
    ("Cutaneous ANTHRAX in Meherpur", "en", ["Anthrax"]),
    ("HPAI H5N1 in poultry", "en", ["Avian Influenza"]),
    ("মেহেরপুরে অ্যানথ্রাক্স রোগী", "bn", ["Anthrax"]),
    ("মেহেরপুরে বাড়ছে অ্যানথাক্সের প্রকোপ", "bn", ["Anthrax"]),
    ("জলাতঙ্কের রোগী বাড়ছে", "bn", ["Rabies"]),
    ("মুরগির খামারে বার্ড ফ্লুর থাবা", "bn", ["Avian Influenza"]),
    ("এইচ৫এন১ ভাইরাস", "bn", ["Avian Influenza"]),
    ("Seasonal influenza vaccination begins", "en", []),
    ("A rabid fan base", "en", []),
    ("Dengue and cholera rise in Dhaka", "en", []),
])
def test_seeded_keywords_work_in_the_filter(text, language, expected):
    _seed()

    assert matches_active_diseases(text, language=language) == expected


@pytest.mark.django_db
@pytest.mark.parametrize("ya", ["য়", "য়"])
def test_bangla_avian_influenza_matches_in_both_encodings(ya):
    _seed()
    text = (
        "এভি" + ya + "ান ইনফ্লু" + ya
        + "েঞ্জা"
    )

    assert matches_active_diseases(text, language="bn") == ["Avian Influenza"]


@pytest.mark.django_db
def test_seeds_the_rzdo_board_unscheduled():
    _seed()

    source = Source.objects.get()
    assert source.name == "RZDO"
    assert source.content_type == Source.ContentType.EIOS
    assert source.url == board_url(API, "1.0", BOARD_ID)
    assert parse_board_url(source.url)[1] == BOARD_ID
    assert source.is_active is False


@pytest.mark.django_db
def test_seeding_twice_changes_nothing():
    _seed()
    keywords = DiseaseKeyword.objects.count()

    output = _seed()

    assert DiseaseKeyword.objects.count() == keywords
    assert Source.objects.count() == 1
    assert "Keywords: 0 created" in output
    assert "Sources: 0 created, 1 already existed." in output


@pytest.mark.django_db
def test_seeding_leaves_existing_keywords_and_a_scheduled_board_alone():
    DiseaseKeyword.objects.create(disease_name="Rabies", keyword="rabies", language="en")
    DiseaseKeyword.objects.create(
        disease_name="Custom", keyword="anthrax", language="", is_active=False,
    )
    Source.objects.create(
        name="My board", url=board_url(API, "1.0", BOARD_ID),
        content_type=Source.ContentType.EIOS, is_active=True,
    )

    _seed()

    assert DiseaseKeyword.objects.filter(keyword="rabies", language="en").exists()
    kept = DiseaseKeyword.objects.get(keyword="anthrax", language="")
    assert (kept.disease_name, kept.is_active) == ("Custom", False)
    source = Source.objects.get()
    assert source.name == "My board"
    assert source.is_active is True


@pytest.mark.django_db
def test_board_is_skipped_with_a_warning_without_an_api_base(settings):
    settings.EIOS_API_BASE_URL = ""

    output = _seed()

    assert "EIOS boards were not added" in output
    assert not Source.objects.exists()
    assert DiseaseKeyword.objects.exists()


@pytest.mark.django_db
def test_seeding_alone_never_crawls():
    with patch("crawler.pipeline.run_for_source") as crawl:
        _seed()

    crawl.assert_not_called()


@pytest.mark.django_db
def test_crawl_option_crawls_the_seeded_board():
    with patch("crawler.pipeline.run_for_source") as crawl:
        output = _seed("--crawl")

    crawl.assert_called_once_with(Source.objects.get().pk)
    assert "Crawling: RZDO" in output
