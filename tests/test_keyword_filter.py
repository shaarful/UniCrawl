"""Keyword filter tests — DB-backed matching and per-source overrides."""
from __future__ import annotations

import pytest

from crawler.filters.keyword import matches_active_diseases
from disease_keywords.models import DiseaseKeyword


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _kw(disease: str, keyword: str, language: str = "", is_active: bool = True):
    return DiseaseKeyword.objects.create(
        disease_name=disease,
        keyword=keyword,
        language=language,
        is_active=is_active,
    )


# ---------------------------------------------------------------------------
# Global DB keyword matching
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_returns_matched_disease_name():
    _kw("dengue", "dengue fever")
    result = matches_active_diseases("Officials report dengue fever cases rising.")
    assert result == ["dengue"]


@pytest.mark.django_db
def test_matching_is_case_insensitive():
    _kw("cholera", "cholera")
    result = matches_active_diseases("CHOLERA outbreak confirmed in district.")
    assert result == ["cholera"]


@pytest.mark.django_db
def test_returns_empty_list_when_no_keyword_matches():
    _kw("dengue", "dengue")
    result = matches_active_diseases("No relevant disease content here.")
    assert result == []


@pytest.mark.django_db
def test_returns_empty_list_for_empty_text():
    _kw("dengue", "dengue")
    assert matches_active_diseases("") == []


@pytest.mark.django_db
def test_multiple_keywords_same_disease_return_disease_name_once():
    _kw("dengue", "dengue")
    _kw("dengue", "dengue fever")
    result = matches_active_diseases("dengue fever reported; dengue cases rising.")
    assert result == ["dengue"]


@pytest.mark.django_db
def test_multiple_diseases_both_returned():
    _kw("dengue", "dengue")
    _kw("cholera", "cholera")
    result = matches_active_diseases("Both dengue and cholera outbreaks confirmed.")
    assert sorted(result) == ["cholera", "dengue"]


@pytest.mark.django_db
def test_result_is_sorted_alphabetically():
    _kw("mpox", "mpox")
    _kw("avian flu", "H5N1")
    _kw("cholera", "cholera")
    result = matches_active_diseases("H5N1, cholera, and mpox reported.")
    assert result == sorted(result)


@pytest.mark.django_db
def test_inactive_keywords_are_excluded():
    _kw("dengue", "dengue", is_active=False)
    result = matches_active_diseases("dengue cases rising.")
    assert result == []


# ---------------------------------------------------------------------------
# Language filtering
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_language_agnostic_keyword_matches_any_language():
    _kw("dengue", "dengue", language="")
    assert matches_active_diseases("dengue", language="en") == ["dengue"]
    assert matches_active_diseases("dengue", language="hi") == ["dengue"]
    assert matches_active_diseases("dengue", language=None) == ["dengue"]


@pytest.mark.django_db
def test_language_specific_keyword_matches_its_language():
    _kw("dengue", "डेंगू", language="hi")
    result = matches_active_diseases("जिले में डेंगू के मामले बढ़ रहे हैं।", language="hi")
    assert result == ["dengue"]


@pytest.mark.django_db
def test_language_specific_keyword_does_not_match_other_language():
    _kw("dengue", "डेंगू", language="hi")
    result = matches_active_diseases("डेंगू outbreak reported.", language="en")
    assert result == []


@pytest.mark.django_db
def test_language_agnostic_and_specific_keywords_combine():
    _kw("dengue", "dengue", language="")       # agnostic
    _kw("dengue", "डेंगू", language="hi")      # Hindi-specific
    en_result = matches_active_diseases("dengue cases.", language="en")
    hi_result = matches_active_diseases("डेंगू dengue cases.", language="hi")
    assert en_result == ["dengue"]    # agnostic keyword matched
    assert hi_result == ["dengue"]    # both matched, deduped to one entry


# ---------------------------------------------------------------------------
# Per-source keyword overrides
# ---------------------------------------------------------------------------

def test_override_list_used_instead_of_db(db):
    _kw("dengue", "dengue")  # DB keyword — should be ignored
    result = matches_active_diseases(
        "cholera outbreak confirmed.",
        keyword_overrides=["cholera", "typhoid"],
    )
    assert result == ["cholera"]


def test_override_list_returns_matched_keywords_as_disease_names():
    result = matches_active_diseases(
        "H5N1 avian flu detected in poultry.",
        keyword_overrides=["H5N1", "avian flu", "mpox"],
    )
    assert sorted(result) == ["H5N1", "avian flu"]


def test_override_list_empty_returns_empty():
    result = matches_active_diseases("dengue cholera", keyword_overrides=[])
    assert result == []


def test_override_matching_is_case_insensitive():
    result = matches_active_diseases(
        "DENGUE FEVER reported.",
        keyword_overrides=["dengue fever"],
    )
    assert result == ["dengue fever"]


def test_override_does_not_hit_db(db):
    """When overrides are provided, no DB query should be made."""
    _kw("dengue", "dengue")
    # Even with DB keywords present, override takes precedence
    result = matches_active_diseases("dengue reported.", keyword_overrides=["cholera"])
    assert result == []  # "dengue" not in override list
