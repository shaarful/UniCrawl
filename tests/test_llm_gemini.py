"""Gemini client tests: response mapping and how each failure is classified."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured
from google.auth.exceptions import RefreshError
from google.genai import errors
from pydantic import ValidationError

from llm.gemini import (
    ArticleError,
    Event,
    Extraction,
    GeminiExtractor,
    ServiceError,
)


@pytest.fixture
def extractor(settings):
    settings.RZDO_GCP_PROJECT = "test-project"
    settings.RZDO_GCP_CREDENTIALS = "key.json"
    with (
        patch("llm.gemini.service_account.Credentials.from_service_account_file"),
        patch("llm.gemini.genai.Client") as client_cls,
    ):
        instance = GeminiExtractor()
    instance.generate = client_cls.return_value.models.generate_content
    return instance


def _response(parsed, text="{}"):
    return SimpleNamespace(
        parsed=parsed,
        text=text,
        usage_metadata=SimpleNamespace(prompt_token_count=11, candidates_token_count=7),
    )


@pytest.mark.parametrize("project, key", [("", "key.json"), ("project", "")])
def test_missing_configuration_fails_fast(settings, project, key):
    settings.RZDO_GCP_PROJECT = project
    settings.RZDO_GCP_CREDENTIALS = key
    with pytest.raises(ImproperlyConfigured):
        GeminiExtractor()


def test_extract_returns_events_and_usage(extractor):
    parsed = Extraction(
        is_disease_event=True,
        events=[Event(disease="anthrax", country="Bangladesh", number_of_cases=12)],
        summary="Twelve anthrax cases.",
    )
    extractor.generate.return_value = _response(parsed, text='{"is_disease_event": true}')

    result = extractor.extract("Title", "Body")

    assert result.extraction.events[0].disease == "anthrax"
    assert (result.prompt_tokens, result.response_tokens) == (11, 7)
    assert result.raw_response == '{"is_disease_event": true}'
    assert result.input_truncated is False


def test_extract_requires_schema_output_at_zero_temperature(extractor):
    extractor.generate.return_value = _response(Extraction(is_disease_event=False, summary="x"))

    extractor.extract("Title", "Body")

    config = extractor.generate.call_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is Extraction
    assert config.temperature == 0


def test_non_event_cannot_keep_events(extractor):
    parsed = Extraction(
        is_disease_event=False, events=[Event(disease="cyclone")], summary="About a cyclone.",
    )
    extractor.generate.return_value = _response(parsed)

    assert extractor.extract("Title", "Body").extraction.events == []


def test_events_without_a_disease_are_dropped(extractor):
    parsed = Extraction(
        is_disease_event=True,
        events=[Event(disease="  "), Event(disease="rabies")],
        summary="Rabies cases.",
    )
    extractor.generate.return_value = _response(parsed)

    events = extractor.extract("Title", "Body").extraction.events

    assert [event.disease for event in events] == ["rabies"]


def test_long_body_is_truncated_and_flagged(extractor, settings):
    settings.LLM_MAX_INPUT_CHARS = 5
    extractor.generate.return_value = _response(Extraction(is_disease_event=False, summary="x"))

    result = extractor.extract("Title", "1234567890")

    contents = extractor.generate.call_args.kwargs["contents"]
    assert "12345" in contents and "123456" not in contents
    assert result.input_truncated is True


def test_response_without_valid_extraction_is_an_article_error(extractor):
    extractor.generate.return_value = _response(None, text=None)

    with pytest.raises(ArticleError):
        extractor.extract("Title", "Body")


def test_bad_request_is_an_article_error(extractor):
    extractor.generate.side_effect = errors.ClientError(400, {"error": {"message": "bad"}})

    with pytest.raises(ArticleError):
        extractor.extract("Title", "Body")


@pytest.mark.parametrize(
    "exc",
    [
        errors.ClientError(401, {"error": {"message": "unauthenticated"}}),
        errors.ClientError(403, {"error": {"message": "denied"}}),
        errors.ClientError(404, {"error": {"message": "no such model"}}),
        errors.ClientError(429, {"error": {"message": "quota"}}),
        errors.ServerError(503, {"error": {"message": "unavailable"}}),
        httpx.ConnectTimeout("timed out"),
        RefreshError("key revoked"),
        ConnectionError("network down"),
    ],
    ids=["401", "403", "404", "429", "503", "timeout", "auth", "network"],
)
def test_outages_are_service_errors(extractor, exc):
    extractor.generate.side_effect = exc

    with pytest.raises(ServiceError):
        extractor.extract("Title", "Body")


def test_slow_response_is_an_article_error_so_it_cannot_repeat_forever(extractor):
    extractor.generate.side_effect = httpx.ReadTimeout("too slow")

    with pytest.raises(ArticleError):
        extractor.extract("Title", "Body")


def test_summary_is_returned(extractor):
    extractor.generate.return_value = _response(
        Extraction(is_disease_event=False, summary="About sport."),
    )

    assert extractor.extract("Title", "Body").extraction.summary == "About sport."


def test_summary_is_required_by_the_schema():
    with pytest.raises(ValidationError):
        Extraction(is_disease_event=False)


def test_blank_summary_is_an_article_error(extractor):
    extractor.generate.return_value = _response(
        Extraction(is_disease_event=False, summary="  "),
    )

    with pytest.raises(ArticleError):
        extractor.extract("Title", "Body")


def test_prompt_asks_for_a_grounded_summary_of_the_required_length(extractor):
    extractor.generate.return_value = _response(Extraction(is_disease_event=False, summary="x"))

    extractor.extract("Title", "Body")

    instruction = extractor.generate.call_args.kwargs["config"].system_instruction
    assert "100 to 200 words" in instruction
    assert "Use only facts stated in the" in instruction
    assert "Never add background" in instruction


def test_prompt_asks_for_a_full_english_translation(extractor):
    extractor.generate.return_value = _response(Extraction(is_disease_event=False, summary="x"))

    extractor.extract("Title", "Body")

    instruction = extractor.generate.call_args.kwargs["config"].system_instruction
    assert "translated_body" in instruction
    assert "Do not summarize" in instruction


def test_unexpected_errors_are_not_hidden(extractor):
    extractor.generate.side_effect = KeyError("bug")

    with pytest.raises(KeyError):
        extractor.extract("Title", "Body")
