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
| `fetchers/eios.py` | `EIOSFetcher` | WHO-EIOS authenticated feed (FR-07) — OAuth2 client credentials, time-windowed board paging |
| `fetchers/social.py` | `SocialFetcher` | Social-media APIs — *stub* |

`HTMLFetcher` and `RSSFetcher` share `HttpFetcher` (`fetchers/base.py`), which uses **httpx** with:
- A `UniCrawl/0.1` User-Agent
- 30-second timeout
- Automatic redirect following (records the final URL separately from the original)
- Non-2xx responses returned as-is — the pipeline inspects `status_code` and raises for 4xx/5xx

Network errors (`httpx.HTTPError`) propagate to the Celery task, which calls `record_failure()`.

#### WHO-EIOS (`fetchers/eios.py`)

An EIOS source's `url` is a board's articles endpoint, e.g.
`https://eios.who.int/portal/api/api/v1.0/Items/matching-board/<boardId>`
(`seed_dev` creates one from `EIOS_BOARD_ID`).

- **Auth:** OAuth2 client-credentials token from Azure AD (`EIOS_TOKEN_URL`, `EIOS_CLIENT_ID`, `EIOS_CLIENT_SECRET`, `EIOS_SCOPE`); cached ~1h, refreshed on expiry or 401.
- **Paging:** the API is offset-paged and refuses offsets past 10,000, so the range is read in `EIOS_WINDOW_MINUTES` windows, halving any window that reports ≥ 10,000 articles.
- **Range:** first run reads the last `EIOS_BACKFILL_HOURS`; later runs resume from `last_crawled_at` minus a 15-minute overlap. Articles already stored for the source are skipped.
- **Full text:** the list endpoint returns only a snippet. Every article is stored with its snippet; for keyword-matched ones the body is replaced by EIOS `fullText` (only returned to accounts with full-text access) or else the original `link` fetched with `HTMLFetcher`. GPHIN (subscription) and social-media links are not fetched. `metadata.body_source` records which was used (`eios_fulltext` / `original_link` / `eios_snippet`).
- **Metadata:** EIOS id, countries, locations, categories, source and translations are kept in `RawArticle.metadata`; English translations also feed the keyword filter (FR-12).
- Articles are streamed page by page, so matches are published while the board is still being read.

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

#### Layer 3 — Semantic Similarity (`dedup/semantic.py`)

Uses a multilingual sentence-transformer model (`paraphrase-multilingual-MiniLM-L12-v2`, 384 dims) to produce an embedding for each article body, then computes cosine similarity against the most recent `CRAWLER_SEMANTIC_MAX_CANDIDATES` stored embeddings in Python. Suppresses articles whose cosine similarity exceeds `CRAWLER_SEMANTIC_DEDUP_THRESHOLD` (default ≥ 0.85).

Enabled via `CRAWLER_SEMANTIC_DEDUP_ENABLED=True` (default `False` — sentence-transformers pulls in PyTorch, ~2 GB). Embeddings are stored in `RawArticle.embedding` as `JSONField(list[float])` for DB portability. **Production upgrade path:** once the PostgreSQL instance has the `pgvector` extension, replace the Python cosine loop with a `CosineDistance` ORM query and an `ivfflat`/`hnsw` index for ANN search.

---

### 6. Keyword Filter (`crawler/filters/keyword.py`)

Matches extracted body text against the active disease keyword list (FR-08). Returns a sorted list of matched canonical disease names, stored in `RawArticle.matched_diseases`. Only matched articles are forwarded downstream.

The keyword list is managed via the `DiseaseKeyword` model (see [Data Models](#data-models)). Matching is case-insensitive substring search. Language-agnostic keywords (empty `language` field) match any article; language-specific keywords only match articles detected in that language.

**Per-source overrides:** setting `Source.keyword_filter_overrides` to a JSON list of keyword strings bypasses the global `DiseaseKeyword` table entirely for that source — useful when a source covers a single disease not yet in the global list.

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
      ├─ 3c. DEDUP — Layer 3 (semantic similarity) [CRAWLER_SEMANTIC_DEDUP_ENABLED=True]
      │       embed(body) → 384-dim vector via sentence-transformer
      │       cosine(query_vec, stored_vecs) ≥ CRAWLER_SEMANTIC_DEDUP_THRESHOLD
      │       Match → write suppressed record (suppression_reason=semantic) → skip
      │       Disabled → skip (embedding remains null; model never loaded)
      │
      ├─ 3d. LANGUAGE DETECTION (FR-12)
      │       langdetect.detect(body) → BCP-47 tag
      │       Failure → fall back to source.language_hint or ""
      │       Non-English content is NOT dropped
      │
      ├─ 3e. KEYWORD FILTER
      │       matches_active_diseases(body, language) → list of disease names
      │       Queries DiseaseKeyword table (or source.keyword_filter_overrides)
      │       No match → processing_status = pending
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
├── metadata            JSONField (null) — source-specific provenance (e.g. EIOS)
├── url_fingerprint     CharField(64, db_index)
├── content_hash        CharField(64, db_index)
├── is_duplicate        BooleanField
├── duplicate_of        FK → self (null)
├── suppression_reason  url | content | semantic
├── embedding           JSONField (null) — 384-dim sentence-transformer vector
├── is_keyword_matched  BooleanField (db_index)
├── matched_diseases    JSONField (null)
├── processing_status   pending | matched | published | suppressed | failed
└── created_at          DateTimeField (auto, db_index)
```

### DiseaseKeyword (`disease_keywords/models.py`)

```
DiseaseKeyword
├── disease_name   CharField(255, db_index) — canonical name returned on match
├── keyword        CharField(255)           — case-insensitive substring to match
├── language       CharField(10, db_index)  — BCP-47; blank = language-agnostic
└── is_active      BooleanField (db_index)
    unique_together: (keyword, language)
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
| `CRAWLER_SEMANTIC_DEDUP_ENABLED` | `False` | Enable Layer 3 semantic dedup (requires sentence-transformers + ~2 GB PyTorch) |
| `CRAWLER_SEMANTIC_DEDUP_THRESHOLD` | `0.85` | FR-23 cosine similarity threshold |
| `CRAWLER_SEMANTIC_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Sentence-transformer model name |
| `CRAWLER_SEMANTIC_MAX_CANDIDATES` | `10000` | Max stored embeddings scanned per dedup check |
| `EIOS_API_BASE_URL` | *(blank)* | WHO-EIOS API base URL (FR-07), e.g. `https://eios.who.int/portal/api` |
| `EIOS_API_VERSION` | `1.0` | EIOS API version path segment |
| `EIOS_TOKEN_URL` | *(blank)* | Azure AD OAuth2 token endpoint for the client-credentials flow |
| `EIOS_CLIENT_ID` | *(blank)* | EIOS consumer client ID |
| `EIOS_CLIENT_SECRET` | *(blank)* | EIOS consumer client secret |
| `EIOS_SCOPE` | *(blank)* | EIOS client-ID scope (`EIOS_CLIENT_ID_SCOPE` in the EIOS docs) |
| `EIOS_BOARD_ID` | *(blank)* | Board whose articles are pulled |
| `EIOS_BACKFILL_HOURS` | `24` | First-run look-back; later runs resume from the last crawl |
| `EIOS_WINDOW_MINUTES` | `60` | Time-window size for paging the board (each window must hold < 10,000 articles; split automatically) |
| `EIOS_PAGE_SIZE` | `100` | Articles per page (API max 100) |

---

## Development Setup

### Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.13 | `python --version` to confirm |
| pipenv | latest | `pip install pipenv` |
| Git | any | |
| Redis | 7+ | Only needed for automated scheduling and publisher. See below. |
| PostgreSQL | 18+ | Optional — SQLite is the default fallback for local dev. |

### 1. Clone and install

```powershell
git clone https://github.com/shaarful/UniCrawl.git
cd UniCrawl

# Install all dependencies (app + dev tools)
pipenv install --dev
```

### 2. Configure environment

`.env` is not committed (it holds local secrets). Create it from the template, which uses SQLite as the DB fallback and sensible defaults:

```powershell
Copy-Item .env.example .env
# Fill in EIOS_CLIENT_ID / EIOS_CLIENT_SECRET / EIOS_SCOPE to enable the EIOS feed
notepad .env
```

Key defaults that let you run without Redis or PostgreSQL:

```
# DATABASE_URL is commented out → falls back to SQLite (db.sqlite3)
CELERY_BROKER_URL=redis://localhost:6379/1   # only needed for background scheduling
REDIS_URL=redis://localhost:6379/0           # only needed when publisher fires
```

### 3. Apply migrations

```powershell
pipenv run python manage.py migrate
```

### 4. Create a superuser

```powershell
pipenv run python manage.py createsuperuser
```

### 5. Seed dev data and run a live crawl

A single command loads disease keywords and three real news sources, then immediately crawls them:

```powershell
pipenv run python manage.py seed_dev --crawl
```

This creates:
- **16 disease keywords** (dengue, cholera, mpox, avian influenza, ebola, and others)
- **3 sources** — Outbreak News Today (RSS), ReliefWeb Health Alerts (RSS), WHO Disease Outbreak News (HTML)

On a clean run it fetches ~30 articles and keyword-matches the disease-relevant ones. Re-running the command is safe — it skips records that already exist. To wipe and start over:

```powershell
pipenv run python manage.py seed_dev --reset --crawl
```

### 6. Start the Django server

```powershell
pipenv run python manage.py runserver
```

### 7. Explore the results

| URL | What you'll find |
|---|---|
| `http://127.0.0.1:8000/health/` | Liveness check — `{"status": "ok"}` |
| `http://127.0.0.1:8000/admin/` | Django admin — browse articles, sources, keywords |
| `http://127.0.0.1:8000/admin/raw_content/rawarticle/` | All crawled articles with status and matched diseases |
| `http://127.0.0.1:8000/admin/sources/source/` | Configured sources with last-crawl timestamps |
| `http://127.0.0.1:8000/admin/disease_keywords/diseasekeyword/` | Active keyword list |
| `http://127.0.0.1:8000/sources/` | REST API for source CRUD |

### Triggering a crawl manually (no Celery needed)

```powershell
pipenv run python manage.py shell
```
```python
from crawler.pipeline import run_for_source
from sources.models import Source

run_for_source(Source.objects.first().id)
```

---

## Running the Full Stack (automated scheduling)

For automated crawls you need Redis running and three processes in separate terminals.

**Start Redis** (Docker is the easiest option on Windows):

```powershell
docker run -d -p 6379:6379 --name unicrawl-redis redis:7-alpine
```

**Three terminals:**

```powershell
# Terminal 1 — Django application server
pipenv run python manage.py runserver

# Terminal 2 — Celery worker (processes crawl_source tasks)
pipenv run celery -A unicrawl worker -l info

# Terminal 3 — Celery beat (fires the scheduler tick every 30 s)
pipenv run celery -A unicrawl beat -l info
```

With all three running, Beat automatically enqueues any source whose `next_crawl_at ≤ now` every 30 seconds. The worker picks them up and runs the full pipeline. Keyword-matched articles are pushed onto the `unicrawl.matched_articles` Redis Stream for the downstream LLM service.

**Health check endpoint:** `GET /health/` returns `{"status": "ok"}` — used by Kubernetes liveness/readiness probes.

**Source management API:** `GET / POST / PUT / DELETE /sources/` — DRF endpoints for Source CRUD (FR-09). New sources default to `is_active=False`; activate them after a successful test-crawl.

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

**Current suite: 146 tests, 0 failures.**

| Test file | What it covers |
|---|---|
| `test_smoke.py` | All modules import cleanly; `/health` endpoint responds with 200 |
| `test_fetchers.py` | HTML + RSS fetchers, redirect tracking, error propagation, headers |
| `test_extractors.py` | trafilatura HTML extraction, feedparser RSS/Atom parsing, boilerplate stripping |
| `test_dedup.py` | URL canonicalization, fingerprinting, content hashing, DB duplicate lookups |
| `test_pipeline.py` | Full fetch→extract→dedup→persist→publish chain, dedup suppression, stub tolerance |
| `test_scheduler.py` | Back-off schedule (1/4/16 min), unavailability threshold, due-source filtering |
| `test_publisher.py` | Redis XADD payload shape, JSON encoding, error propagation, connection reuse |
| `test_keyword_filter.py` | DB keyword matching, case-insensitivity, language filtering, per-source overrides |
| `test_eios.py` | EIOS token caching/refresh, windowed paging + split at 10k, full-text fallback chain, re-poll skipping |
| `test_semantic_dedup.py` | Cosine math, embedding computation, DB duplicate detection, pipeline integration |

---

## Open Questions (TBD)

These items from SRS §7.3 are deliberately unresolved upstream and must be answered before the corresponding components can be finalized:

| ID | Status | Question |
|---|---|---|
| **TBD-01** | Open | Final regional language list beyond the initial South Asian set (blocks FR-12 full coverage) |
| **TBD-02** | **Resolved** | Disease list stored in `DiseaseKeyword` DB table, managed via Django admin / DRF API |
| **TBD-03** | Open | Final extraction-field list for the LLM service (affects publisher payload schema) |
| **TBD-04** | Open | LLM deployment option — in-house fine-tuned model vs. third-party API (affects queue payload) |

**Stub components** (raise `NotImplementedError`; the pipeline handles each gracefully without crashing):

- `crawler/fetchers/social.py` — `SocialFetcher.fetch()` — per-platform implementations pending
