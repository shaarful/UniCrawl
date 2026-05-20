# UniCrawl — Universal Web Crawler Service

UniCrawl is the autonomous ingestion component of the **Regional Zoonotic Disease Observatory and Early Warning System (RZDO-EWS)**. It crawls administrator-configured open-source URLs — news portals, RSS feeds, government health bulletins, and social-media APIs — filters content against an active disease keyword list, and hands matched articles to a downstream LLM extraction pipeline within 60 seconds.

This service covers **FR-07 through FR-14** of the RZDO-EWS SRS (v1.5). All other microservices (LLM extraction, validation/moderation, analytics, dashboard) are out of scope here.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Component Reference](#component-reference)
3. [Full Crawl Workflow](#full-crawl-workflow)
4. [Data Models](#data-models)
5. [Tech Stack](#tech-stack)
6. [Configuration Reference](#configuration-reference)
7. [Development Setup](#development-setup)
8. [Running the Service](#running-the-service)
9. [Running Tests](#running-tests)
10. [Open Questions (TBD)](#open-questions-tbd)

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          UniCrawl Service                               │
│                                                                         │
│  ┌──────────┐  every 30s  ┌────────────┐  per source  ┌─────────────┐  │
│  │  Celery  │────────────▶│ Scheduler  │─────────────▶│   Fetcher   │  │
│  │   Beat   │             │  tick task │ crawl_source │  HTML / RSS │  │
│  └──────────┘             └────────────┘     task     │  EIOS / API │  │
│                                  │                    └──────┬──────┘  │
│                                  │ back-off / unavailable    │          │
│                                  ▼                           │          │
│                         ┌──────────────┐                     │          │
│                         │  Source DB   │◀─── crawl state ────┘          │
│                         │  (Postgres)  │     next_crawl_at               │
│                         └──────────────┘     failures                    │
│                                                                         │
│  ┌─────────────────────────── Pipeline ──────────────────────────────┐  │
│  │                                                                   │  │
│  │  Fetch ──▶ Extract ──▶ Dedup ──▶ Lang ──▶ Keyword ──▶ Store ──▶ Pub │  │
│  │           (text /     L1: URL   Detect   Filter    Raw DB  Redis  │  │
│  │           RSS items)  L2: Hash           (TBD-02)         Stream  │  │
│  │                       L3: Sem.                                    │  │
│  └───────────────────────────────────────────────────────────────────┘  │
│                                                                         │
│  ┌────────────┐   ┌──────────────────┐   ┌───────────────────────────┐  │
│  │  /health   │   │ Django REST API  │   │  Redis Stream             │  │
│  │ (liveness) │   │  Source CRUD     │   │  unicrawl.matched_articles│  │
│  └────────────┘   └──────────────────┘   │  ────────────────────────▶│  │
│                                          │  LLM Processing Service   │  │
│                                          └───────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Component Reference

### 1. Source Registry (`sources/`)

The **Source** model is the master list of all URLs the crawler ingests. Administrators manage it through the Django REST Framework API or the Django admin.

| Field | Purpose |
|---|---|
| `url` | The target URL to crawl |
| `content_type` | `html`, `rss`, `eios`, or `social` — determines which fetcher is used |
| `crawl_interval_seconds` | How often to crawl this source (default: 3600 s) |
| `crawl_depth` | Link-follow depth; 1 = landing page only |
| `keyword_filter_overrides` | Per-source disease keyword list; `null` = use global list |
| `language_hint` | BCP-47 hint (e.g. `hi`, `bn`); blank = auto-detect |
| `is_active` | Only active sources are scheduled; requires a passing test-crawl first (FR-09) |
| `next_crawl_at` | Computed after each run; scheduler uses this to gate enqueuing |
| `consecutive_failures` | Incremented by `record_failure()`; resets to 0 on success |
| `is_temporarily_unavailable` | Set after 3 failed retries; admin must reset manually |

---

### 2. Scheduler (`crawler/scheduler.py` + `crawler/tasks.py`)

Two Celery tasks drive all crawl activity:

**`crawler.tasks.tick`** — Called by Celery beat every 30 seconds. Invokes `schedule_due_sources()`, which queries the Source table for all active, non-unavailable sources whose `next_crawl_at ≤ now` (or is `NULL`), and enqueues a `crawl_source` task for each.

**`crawler.tasks.crawl_source(source_id)`** — Runs the full pipeline for one source. On any unhandled exception it calls `record_failure(source_id)` to advance the back-off state, then re-raises so Celery marks the task as `FAILED`.

**Back-off logic (FR-14):**

```
Initial failure  →  retry after  1 minute   (consecutive_failures = 1)
2nd failure      →  retry after  4 minutes  (consecutive_failures = 2)
3rd failure      →  retry after 16 minutes  (consecutive_failures = 3)
4th failure      →  is_temporarily_unavailable = True  (admin must re-enable)
```

Intervals are configured via `CRAWLER_RETRY_BACKOFF_MINUTES` (default `[1, 4, 16]`). The `select_for_update()` transaction in `record_failure` prevents race conditions when multiple workers process the same source concurrently.

---

### 3. Fetchers (`crawler/fetchers/`)

Each fetcher implements the `Fetcher` protocol: `fetch(url) → FetchResult`.

| Module | Class | Used for |
|---|---|---|
| `fetchers/html.py` | `HTMLFetcher` | News portals, government bulletins |
| `fetchers/rss.py` | `RSSFetcher` | RSS / Atom feeds |
| `fetchers/eios.py` | `EIOSFetcher` | WHO-EIOS authenticated feed (FR-07) — *stub* |
| `fetchers/social.py` | `SocialFetcher` | Social-media APIs — *stub* |

`HTMLFetcher` and `RSSFetcher` share `HttpFetcher` (`fetchers/base.py`), which uses **httpx** with:
- A `UniCrawl/0.1` User-Agent
- 30-second timeout
- Automatic redirect following (records the final URL separately from the original)
- Non-2xx responses returned as-is — the pipeline inspects `status_code` and raises for 4xx/5xx

Network errors (`httpx.HTTPError`) propagate to the Celery task, which calls `record_failure()`.

---

### 4. Extractor (`crawler/extractors/base.py`)

Turns raw fetched bytes into structured text.

**HTML sources** — [trafilatura](https://trafilatura.readthedocs.io/) strips boilerplate (nav, ads, footers) and returns the main article body. Metadata (title, publish date) is extracted separately via `trafilatura.extract_metadata`. A BeautifulSoup `<title>` tag fallback fires when trafilatura yields no title.

**RSS / Atom sources** — [feedparser](https://feedparser.readthedocs.io/) parses the feed. Each `<entry>` / `<item>` becomes a separate `FeedItem` with its own URL, title, body (HTML stripped to plain text via BeautifulSoup), and publish date. Entries without a usable link or ID are skipped because they cannot be deduplicated or re-fetched.

Language detection is intentionally **not** done here — it is the pipeline's responsibility (FR-12), keeping extraction side-effect-free and deterministic.

---

### 5. Dedup Pipeline (`crawler/dedup/`)

Three chained layers catch duplicates before storage (FR-11, FR-23). Each layer that fires creates a `RawArticle` record with `processing_status = suppressed`, an audit link (`duplicate_of`) pointing to the original, and a `suppression_reason`.

#### Layer 1 — URL Fingerprint (`dedup/url_fingerprint.py`)

Canonicalizes the URL before hashing:
- Lowercases scheme and host
- Strips leading `www.`
- Removes default ports (80 for http, 443 for https)
- Drops URL fragments (`#section`)
- Strips tracking parameters (`utm_*`, `fbclid`, `gclid`, `ref`, and others)
- Sorts remaining query parameters
- Removes trailing slashes from non-root paths

SHA-256 of the canonical URL → 64-character hex fingerprint. Two URLs that differ only in tracking noise produce the same fingerprint and are treated as the same article.

#### Layer 2 — Content Hash (`dedup/content_hash.py`)

Normalizes extracted body text (collapse whitespace runs, strip, lowercase), then SHA-256-hashes. Catches syndicated reposts of identical articles appearing at different URLs.

#### Layer 3 — Semantic Similarity (`dedup/semantic.py`) — *stub*

Will use sentence-transformer embeddings compared against a pgvector ANN index. Suppresses near-duplicate articles whose cosine similarity is above a configurable threshold (default ≥ 0.85, set via `CRAWLER_SEMANTIC_DEDUP_THRESHOLD`). Not yet implemented.

---

### 6. Keyword Filter (`crawler/filters/keyword.py`) — *stub*

Matches extracted body text against the active disease keyword list (FR-08). Returns the list of matched disease names, stored in `RawArticle.matched_diseases`. Only matched articles are forwarded downstream.

Blocked by **TBD-02** (the disease list source is not yet finalized). While the stub raises `NotImplementedError`, the pipeline handles it gracefully — articles are persisted with `processing_status = pending` rather than crashing. Per-source overrides can be configured in `Source.keyword_filter_overrides`.

---

### 7. Raw Store (`raw_content/`)

Every article that passes fetching and extraction is written to `RawArticle` **before** any downstream processing (FR-13). The record is immutable after creation; only `processing_status` and dedup/match fields are updated as the pipeline progresses.

**Processing status lifecycle:**

```
pending     →  fetched, waiting for dedup + keyword filter
matched     →  keyword matched, waiting to be published to LLM queue
published   →  successfully handed off to LLM queue (FR-08 ≤60s SLA met)
suppressed  →  duplicate detected; audit link set to original article
failed      →  unrecoverable pipeline error; see structured error log
```

---

### 8. Publisher (`crawler/publisher.py`)

Appends keyword-matched articles to a **Redis Stream** (`LLM_QUEUE_NAME`, default `unicrawl.matched_articles`) using `XADD`. At-least-once delivery is provided by the Redis Streams consumer-group model on the LLM service side (`XREADGROUP` / `XACK`). The article's `processing_status` only advances to `published` after a confirmed `XADD`.

**Stream message fields** (all strings — Redis Streams requirement):

| Field | Value |
|---|---|
| `article_id` | Primary key in the RawArticle table |
| `source_id` | Source FK |
| `url` / `final_url` | Article URLs |
| `title` / `body` | Extracted content |
| `language` | BCP-47 tag detected by langdetect |
| `matched_diseases` | JSON-encoded list — e.g. `["dengue", "cholera"]` |
| `fetched_at` | ISO-8601 timestamp of the crawl |
| `published_at` | ISO-8601 timestamp of the stream publish |

A `redis.RedisError` propagates unchanged so the pipeline leaves the article in `matched` state for a future retry.

---

## Full Crawl Workflow

```
Celery Beat (every 30 s)
        │
        ▼
schedule_due_sources()
  Query: is_active=True, is_temporarily_unavailable=False,
         next_crawl_at ≤ now  OR  next_crawl_at IS NULL
  For each due source → crawl_source.delay(source_id)
        │
        ▼  (Celery worker picks up task)
crawl_source(source_id)
        │
        ├── on any exception ──▶ record_failure(source_id)
        │                           consecutive_failures += 1
        │                           if failures ≤ 3:
        │                               next_crawl_at = now + backoff[failures-1]
        │                           else:
        │                               is_temporarily_unavailable = True
        │                        re-raise → Celery marks task FAILED
        │
        ▼  (success path)
run_for_source(source_id)
  │
  ├─ 1. FETCH
  │       Select fetcher by source.content_type
  │       HTTP GET → FetchResult(url, status_code, body, headers, final_url)
  │       status_code ≥ 400 → raise RuntimeError → back-off
  │
  ├─ 2. EXTRACT  (produces a list of candidate articles)
  │       HTML  → 1 article (trafilatura strips boilerplate)
  │       RSS   → N articles (one per feed entry with its own URL)
  │
  └─ 3. FOR EACH CANDIDATE ARTICLE:
      │
      ├─ 3a. DEDUP — Layer 1 (URL fingerprint)
      │       fingerprint(url) → query RawArticle.url_fingerprint
      │       Match → write suppressed record (suppression_reason=url) → skip
      │
      ├─ 3b. DEDUP — Layer 2 (content hash)
      │       sha256(normalize(body)) → query RawArticle.content_hash
      │       Match → write suppressed record (suppression_reason=content) → skip
      │
      ├─ 3c. DEDUP — Layer 3 (semantic similarity) [not yet implemented]
      │       cosine(embed(body), stored embeddings) ≥ threshold
      │       Match → write suppressed record (suppression_reason=semantic) → skip
      │
      ├─ 3d. LANGUAGE DETECTION (FR-12)
      │       langdetect.detect(body) → BCP-47 tag
      │       Failure → fall back to source.language_hint or ""
      │       Non-English content is NOT dropped
      │
      ├─ 3e. KEYWORD FILTER [stub — TBD-02]
      │       matches_active_diseases(body, language) → list of disease names
      │       NotImplementedError → save as pending, no crash
      │
      ├─ 3f. PERSIST RAW (FR-13)
      │       RawArticle.objects.create(url, body, fingerprints, language, ...)
      │       processing_status = matched  (if keyword hit)
      │                         = pending  (no hit or filter not yet wired)
      │
      └─ 3g. PUBLISH to LLM Queue (FR-08, ≤60s SLA)
              if is_keyword_matched:
                  Redis XADD(LLM_QUEUE_NAME, fields)  →  msg_id returned
                  processing_status → published
                  RedisError → propagate; article stays matched for retry
                  NotImplementedError (stub) → article stays matched, no crash

      After all articles processed → UPDATE SOURCE
          last_crawled_at       = now
          next_crawl_at         = now + source.crawl_interval_seconds
          consecutive_failures  = 0
          is_temporarily_unavailable = False
```

---

## Data Models

### Source (`sources/models.py`)

```
Source
├── name                        CharField (unique)
├── url                         URLField (unique)
├── content_type                html | rss | eios | social
├── language_hint               BCP-47, blank = auto-detect
├── crawl_interval_seconds      PositiveIntegerField, default 3600
├── crawl_depth                 PositiveSmallIntegerField, default 1
├── keyword_filter_overrides    JSONField (null = use global list)
├── is_active                   BooleanField, default False
├── notes                       TextField
├── last_crawled_at             DateTimeField (null)
├── next_crawl_at               DateTimeField (null, db_index)
├── consecutive_failures        PositiveSmallIntegerField, default 0
├── is_temporarily_unavailable  BooleanField, default False
├── unavailable_until           DateTimeField (null)
└── added_by                    FK → AUTH_USER_MODEL
```

### RawArticle (`raw_content/models.py`)

```
RawArticle
├── source              FK → Source (PROTECT)
├── url                 URLField  — original request URL
├── final_url           URLField  — URL after redirects
├── http_status         PositiveSmallIntegerField
├── fetched_at          DateTimeField
├── title               CharField(1024)
├── body                TextField
├── language            CharField(10)  — BCP-47
├── url_fingerprint     CharField(64, db_index)
├── content_hash        CharField(64, db_index)
├── is_duplicate        BooleanField
├── duplicate_of        FK → self (null)
├── suppression_reason  url | content | semantic
├── is_keyword_matched  BooleanField (db_index)
├── matched_diseases    JSONField (null)
├── processing_status   pending | matched | published | suppressed | failed
└── created_at          DateTimeField (auto, db_index)
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.13 (prod target: 3.14 on Ubuntu 26.04 LTS) |
| Web framework | Django 6.x + Django REST Framework |
| Task queue | Celery 5.x with Redis broker |
| Beat scheduler | Celery beat (polls for due sources every 30 s) |
| HTTP client | httpx with HTTP/2 support |
| HTML extraction | trafilatura + BeautifulSoup4 + lxml |
| Feed parsing | feedparser |
| Language detection | langdetect |
| Database | PostgreSQL 18+ with PostGIS 3.x |
| Message queue | Redis Streams (at-least-once delivery) |
| Structured logging | structlog |
| Deployment | Docker + Kubernetes 1.28+, on-prem |

---

## Configuration Reference

All settings are environment-variable-driven via `django-environ`. Copy `.env.example` to `.env` and adjust.

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SECRET_KEY` | *(required in prod)* | Django secret key |
| `DJANGO_DEBUG` | `False` | Debug mode |
| `DJANGO_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated allowed hosts |
| `DATABASE_URL` | SQLite fallback | PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis for publisher + Django cache |
| `CELERY_BROKER_URL` | `memory://` | Celery message broker |
| `CELERY_RESULT_BACKEND` | `cache+memory://` | Celery result store |
| `LLM_QUEUE_NAME` | `unicrawl.matched_articles` | Redis Stream key for LLM handoff |
| `CRAWLER_DEFAULT_INTERVAL_SECONDS` | `3600` | Default per-source crawl interval |
| `CRAWLER_RETRY_BACKOFF_MINUTES` | `1,4,16` | FR-14 retry intervals (comma-separated) |
| `CRAWLER_SEMANTIC_DEDUP_THRESHOLD` | `0.85` | FR-23 cosine similarity threshold |
| `EIOS_API_BASE_URL` | *(blank)* | WHO-EIOS base URL (FR-07) |
| `EIOS_API_TOKEN` | *(blank)* | WHO-EIOS bearer token (FR-07) |

---

## Development Setup

**Prerequisites:** Python 3.13, pipenv, PostgreSQL (or use the SQLite fallback for local dev), Redis.

```powershell
# Clone and enter the repo
git clone <repo-url>
cd UniCrawl

# Install all dependencies (including dev)
pipenv install --dev

# Activate the virtual environment
pipenv shell

# Copy and edit environment config
cp .env.example .env

# Apply database migrations
python manage.py migrate

# Create a Django superuser (for the admin UI)
python manage.py createsuperuser
```

---

## Running the Service

Three processes must run concurrently:

```bash
# 1. Django application server
python manage.py runserver

# 2. Celery worker — processes crawl_source tasks
celery -A unicrawl worker -l info

# 3. Celery beat — fires the tick task every 30 s
celery -A unicrawl beat -l info
```

**Health check endpoint:** `GET /health/` returns `{"status": "ok"}` — used by Kubernetes liveness/readiness probes.

**Source management API:** `GET / POST / PUT / DELETE /sources/` — DRF endpoints for administrator CRUD on the Source registry (FR-09). New sources default to `is_active=False`; activate them after a successful test-crawl.

---

## Running Tests

```powershell
# Full suite
pipenv run pytest

# Single module with verbose output
pipenv run pytest tests/test_pipeline.py -v

# With coverage report
pipenv run pytest --cov=crawler --cov=sources --cov=raw_content
```

**Current suite: 96 tests, 0 failures.**

| Test file | What it covers |
|---|---|
| `test_smoke.py` | All modules import cleanly; `/health` endpoint responds with 200 |
| `test_fetchers.py` | HTML + RSS fetchers, redirect tracking, error propagation, headers |
| `test_extractors.py` | trafilatura HTML extraction, feedparser RSS/Atom parsing, boilerplate stripping |
| `test_dedup.py` | URL canonicalization, fingerprinting, content hashing, DB duplicate lookups |
| `test_pipeline.py` | Full fetch→extract→dedup→persist→publish chain, dedup suppression, stub tolerance |
| `test_scheduler.py` | Back-off schedule (1/4/16 min), unavailability threshold, due-source filtering |
| `test_publisher.py` | Redis XADD payload shape, JSON encoding, error propagation, connection reuse |

---

## Open Questions (TBD)

These items from SRS §7.3 are deliberately unresolved upstream and must be answered before the corresponding components can be finalized:

| ID | Blocks | Question |
|---|---|---|
| **TBD-01** | Language support (FR-12) | Final regional language list beyond the initial South Asian set |
| **TBD-02** | `crawler/filters/keyword.py` | Disease list source — DB table, YAML file, or external API? |
| **TBD-03** | Publisher payload | Final extraction-field list for the LLM service |
| **TBD-04** | Queue payload schema | LLM deployment option (in-house fine-tuned model vs. third-party API) |

**Stub components** (raise `NotImplementedError`; the pipeline handles each gracefully without crashing):

- `crawler/filters/keyword.py` — `matches_active_diseases()` — blocked by TBD-02
- `crawler/dedup/semantic.py` — `similarity_score()`, `is_duplicate()` — needs sentence-transformers + pgvector
- `crawler/fetchers/eios.py` — `EIOSFetcher.fetch()` — needs EIOS API credentials (FR-07)
- `crawler/fetchers/social.py` — `SocialFetcher.fetch()` — per-platform implementations pending
