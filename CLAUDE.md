# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

UniCrawl is the **universal web crawler** component of the **Regional Zoonotic Disease Observatory and Early Warning System (RZDO-EWS)** described in `srs/RZDO_EWS_SRS_v1.5.docx`. It autonomously collects disease-related content from administrator-defined open-source URLs (news portals, RSS feeds, government health bulletins, social media APIs) and forwards keyword-matched content to a downstream LLM extraction pipeline.

The Crawler Service is one of five microservices in the broader RZDO-EWS architecture. **In scope here are the crawler and LLM extraction** — validation/moderation, analytics, and the dashboard are separate services and not implemented in this repo.

LLM extraction lives in the `llm` app by deliberate choice (the SRS lists it as a separate service, and the SRS is to be updated to match). Keep it easy to split out later: it runs as its own process (`manage.py run_llm_worker`), gets articles only through the Redis stream, and the dependency stays one-way, so `crawler` must never import `llm`.

The SRS is the authoritative requirements document. Read it (`srs/RZDO_EWS_SRS_v1.5.docx`) before making architectural decisions. The crawler-specific requirements are FR-07 through FR-14 in §3.2.

## Crawler Scope and Responsibilities

What the crawler must do (per SRS §3.2):

- **Source-agnostic ingestion (FR-08):** Pull from any administrator-configured URL, auto-detecting whether it's a news portal, RSS feed, bulletin, or social API.
- **Per-source admin interface (FR-09):** CRUD on sources with per-source crawl frequency, depth, content type, and keyword-filter overrides. New sources must be test-crawled and validated before activation.
- **Configurable per-source scheduling (FR-10):** Honor each source's interval; do not crawl early.
- **Keyword filtering against active disease list:** Only matched content goes downstream. The active disease list and extraction fields are TBD (TBD-02, TBD-03 in the SRS).
- **Duplicate suppression (FR-11, FR-23):** Multi-layer — URL fingerprinting → content hash → semantic similarity (default threshold ≥ 0.85). Duplicates are linked to the original and audit-logged.
- **Multilingual retrieval (FR-12):** Support South Asian regional languages during crawling and search; do not drop non-English content.
- **Raw store (FR-13):** Persist `(URL, title, body, metadata, timestamp)` with provenance before any downstream processing.
- **Error handling (FR-14):** Retry failed sources up to 3 times with exponential back-off at **1, 4, and 16 minutes**, then mark temporarily unavailable.
- **Authenticated feed (FR-07):** Also pulls from EIOS (WHO Event Information Site) via its API/RSS on schedule.

Hand-off contract: keyword-matched raw content must reach the LLM processing pipeline **within 60 seconds** of a crawl cycle being triggered (FR-08).

## Non-Functional Targets

- **Throughput:** ≥ 10,000 articles/day across all configured sources (NFR-03).
- **Constraint:** Only publicly accessible / authenticated sources may be ingested (SRS §2.5). No anti-bot circumvention.
- **Logging:** Structured JSON logs with timestamp, service, error code, stack trace. All moderation/duplicate decisions are audit-logged.
- **Health:** Expose a `/health` endpoint for liveness/readiness probes.
- **Style:** PEP 8; unit test coverage target ≥ 80% for backend services.

## Tech Stack (per SRS §2.4)

- **Runtime:** Python 3.14+ on Linux (Ubuntu 26.04 LTS) in the production target. The local `Pipfile` currently pins **3.13.3** — confirm with the user before bumping to 3.14 since CPython 3.14 may not be released yet on their dev machine.
- **Framework:** Django 6.x + Django REST Framework (the SRS prescribes this stack project-wide; for a standalone crawler service a lighter framework may also be reasonable — flag the trade-off rather than silently choosing).
- **Database:** PostgreSQL 18+ with PostGIS 3.x (raw-content store lives here; geospatial fields are populated downstream by LLM extraction).
- **Message queue:** Redis Streams or Apache Kafka (at-least-once delivery) for handing matched content to the LLM service.
- **Deployment:** Docker + Kubernetes 1.28+, on-prem target.

## Environment Setup

```powershell
pipenv install          # Install dependencies
pipenv install --dev    # Install dev dependencies
pipenv shell            # Activate virtual environment
```

The `Pipfile` is currently empty — dependencies must be added as the crawler is implemented (e.g., `requests`/`httpx`, `feedparser`, `beautifulsoup4`, `playwright` or `selenium` for JS-heavy sites, `langdetect`, `simhash` or sentence-transformers for similarity, `django`, `djangorestframework`, `psycopg`, `redis`/`kafka-python`).

## Architecture Notes for Future Work

When the codebase grows, the crawler naturally decomposes into:

1. **Source registry** — DB-backed list of configured URLs with per-source config (admin CRUD via DRF).
2. **Scheduler** — fires crawl jobs per source's interval; respects back-off state.
3. **Fetchers** — pluggable per content type: generic HTML, RSS/Atom, EIOS API, social APIs.
4. **Extractor** — pulls main text body from fetched pages (e.g., trafilatura/readability).
5. **Keyword filter** — matches against the active disease list.
6. **Dedup pipeline** — URL fingerprint → content hash → semantic similarity (chained as in FR-23).
7. **Raw store** — Postgres tables for raw records and provenance.
8. **Outbound publisher** — pushes matched records onto the queue for the LLM service.
9. **Observability** — structured logs, `/health`, retry/back-off state, dead-letter queue.

## Open Questions (TBD per SRS §7.3)

These are deliberately unspecified upstream — surface them to the user when relevant rather than guessing:

- TBD-01: Final regional language list beyond initial South Asian set.
- TBD-02: The disease list (drives the keyword filter).
- TBD-03: Final extraction-field list (affects what metadata the crawler must preserve for LLM use).
- TBD-04: LLM deployment option (in-house fine-tuned vs. third-party API) — doesn't affect the crawler boundary but affects the queue payload schema.
