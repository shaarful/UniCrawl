"""The table behind each number on the crawl run page: articles by type of site.

The database does the counting, so the cost of a table depends on how many articles the run
has only inside the database. The result has one row per type of site, however many sites.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from django.db.models import BooleanField, Case, Count, Q, Value, When

from crawler.site_types import GPHIN_SOURCE_IDS, site_type
from raw_content.models import RawArticle
from sources.models import Source

FULL_TEXT_SOURCES = ("original_link", "eios_fulltext")

# Tiles whose articles are stored, so they can be read back from the database.
_STORED_ARTICLES = {
    "processed": Q(),
    "full_text": Q(metadata__body_source__in=FULL_TEXT_SOURCES),
    "new": Q(is_duplicate=False),
    "matched": Q(is_keyword_matched=True),
    "published": Q(processing_status=RawArticle.ProcessingStatus.PUBLISHED),
    "duplicates": Q(is_duplicate=True),
}
TILES = frozenset({*_STORED_ARTICLES, "skipped", "errors"})

_DUPLICATE_REASONS = dict(RawArticle.SuppressionReason.choices)


def run_articles(run):
    """The articles a run stored. They share its `fetched_at`."""
    if not run.fetched_at:
        return RawArticle.objects.none()
    return RawArticle.objects.filter(
        source_id=run.source_id,
        created_at__gte=run.started_at,
        fetched_at=run.fetched_at,
    )


def _reason_list(reasons: Counter) -> list[dict]:
    return [
        {"reason": reason, "count": count}
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    ]


def _stored_rows(run, tile: str) -> list[dict]:
    rows: dict[str, dict] = defaultdict(lambda: {
        "articles": 0, "our_crawl": 0, "eios_text": 0, "snippet": 0, "reasons": Counter(),
    })
    groups = (
        run_articles(run)
        .filter(_STORED_ARTICLES[tile])
        .annotate(is_gphin=Case(
            When(metadata__eios__source__id__in=GPHIN_SOURCE_IDS, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ))
        .order_by()
        .values(
            "source_type", "is_gphin", "metadata__body_source", "metadata__link_error",
            "suppression_reason",
        )
        .annotate(articles=Count("id"))
    )
    for group in groups:
        count = group["articles"]
        body_source = group["metadata__body_source"]
        row = rows[site_type(group["source_type"], group["is_gphin"])]
        row["articles"] += count
        if body_source == "original_link":
            row["our_crawl"] += count
        elif body_source == "eios_fulltext":
            row["eios_text"] += count
        elif body_source == "eios_snippet":
            row["snippet"] += count
        if tile == "duplicates":
            reason = group["suppression_reason"]
            row["reasons"][_DUPLICATE_REASONS.get(reason, reason)] += count
        elif body_source == "eios_snippet" and group["metadata__link_error"]:
            row["reasons"][group["metadata__link_error"]] += count
    return [
        {**row, "site_type": name, "reasons": _reason_list(row["reasons"])}
        for name, row in rows.items()
    ]


def _recorded_rows(run, tile: str) -> list[dict]:
    if tile == "skipped":
        return [
            {"site_type": name, "articles": count, "reasons": []}
            for name, count in run.skipped_by_type.items()
        ]
    return [
        {
            "site_type": name,
            "articles": sum(reasons.values()),
            "reasons": _reason_list(Counter(reasons)),
        }
        for name, reasons in run.errors_by_type.items()
    ]


def summarize(run, tile: str) -> dict:
    """The table for one tile, with what could not be crawled first. `tile` must be in TILES."""
    if tile in _STORED_ARTICLES:
        rows = _stored_rows(run, tile)
        recorded = True
        reason_label = "Why duplicate" if tile == "duplicates" else "Why not crawled"
        show_text_sources = run.source.content_type == Source.ContentType.EIOS
    else:
        rows = _recorded_rows(run, tile)
        # Runs from before these were recorded have a count on the tile but no types.
        recorded = bool(rows) or not getattr(run, tile)
        reason_label = "Errors"
        show_text_sources = False
    rows.sort(key=lambda row: (-row.get("snippet", 0), -row["articles"], row["site_type"]))
    return {
        "rows": rows,
        "total": sum(row["articles"] for row in rows),
        "tile_count": getattr(run, tile),
        "is_running": run.is_running,
        "recorded": recorded,
        "reason_label": reason_label,
        "show_text_sources": show_text_sources,
    }
