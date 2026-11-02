"""Gemini client tests: response mapping and how each failure is classified."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from django.core.exceptions import ImproperlyConfigured
from google.auth.exceptions import RefreshError
from google.genai import errors

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
    )
    extractor.generate.return_value = _response(parsed, text='{"is_disease_event": true}')

    result = extractor.extract("Title", "Body")

    assert result.extraction.events[0].disease == "anthrax"
    assert (result.prompt_tokens, result.response_tokens) == (11, 7)
    assert result.raw_response == '{"is_disease_event": true}'
    assert result.input_truncated is False


def test_extract_requires_schema_output_at_zero_temperature(extractor):
    extractor.generate.return_value = _response(Extraction(is_disease_event=False))

    extractor.extract("Title", "Body")

    config = extractor.generate.call_args.kwargs["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is Extraction
    assert config.temperature == 0


def test_non_event_cannot_keep_events(extractor):
    parsed = Extraction(is_disease_event=False, events=[Event(disease="cyclone")])
    extractor.generate.return_value = _response(parsed)

    assert extractor.extract("Title", "Body").extraction.events == []


def test_events_without_a_disease_are_dropped(extractor):
    parsed = Extraction(
        is_disease_event=True,
        events=[Event(disease="  "), Event(disease="rabies")],
    )
    extractor.generate.return_value = _response(parsed)

    events = extractor.extract("Title", "Body").extraction.events

    assert [event.disease for event in events] == ["rabies"]


def test_long_body_is_truncated_and_flagged(extractor, settings):
    settings.LLM_MAX_INPUT_CHARS = 5
    extractor.generate.return_value = _response(Extraction(is_disease_event=False))

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


def test_unexpected_errors_are_not_hidden(extractor):
    extractor.generate.side_effect = KeyError("bug")

    with pytest.raises(KeyError):
        extractor.extract("Title", "Body")
