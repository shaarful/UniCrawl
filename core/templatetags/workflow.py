"""The "what to do next" guide on the crawl dashboard and the admin home page.

It counts across apps, so it lives in the project package. That keeps the crawler
free of any `llm` import.
"""
from __future__ import annotations

from django import template
from django.urls import reverse

from crawler.models import CrawlRun
from disease_keywords.models import DiseaseKeyword
from llm.models import ArticleExtraction, ExtractedEvent
from raw_content.models import RawArticle
from sources.models import Source

register = template.Library()


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def build_steps() -> list[dict]:
    """The steps in order, each marked done, and the first unfinished one marked next."""
    keywords = DiseaseKeyword.objects.filter(is_active=True).count()
    sources = Source.objects.count()
    # A source with its own keyword list does not use the shared keywords.
    has_overrides = (
        Source.objects.filter(keyword_filter_overrides__isnull=False)
        .exclude(keyword_filter_overrides=[])
        .exists()
    )
    runs = CrawlRun.objects.count()
    stored = RawArticle.objects.count()
    eligible = RawArticle.objects.filter(is_keyword_matched=True, is_duplicate=False)
    matched = eligible.count()
    # Retrying and failed extractions still need attention, so only completed ones count.
    completed = ArticleExtraction.Status.COMPLETED
    waiting = eligible.exclude(extraction__status=completed).count()
    processed = ArticleExtraction.objects.filter(status=completed).count()
    events = ExtractedEvent.objects.count()

    articles_url = reverse("admin:raw_content_rawarticle_changelist")
    if waiting:
        llm_status = f"{waiting:,} of {matched:,} matched not processed"
    elif matched:
        llm_status = f"All {matched:,} matched processed"
    elif processed:
        llm_status = f"{processed:,} processed"
    else:
        llm_status = "Nothing matched yet"

    steps = [
        {
            "title": "Disease keywords",
            "hint": "Only articles containing these go to the LLM. Add them before crawling.",
            "status": _plural(keywords, "active keyword"),
            "done": bool(keywords) or has_overrides,
            "action": "Add keywords",
            "url": reverse("admin:disease_keywords_diseasekeyword_changelist"),
        },
        {
            "title": "Sources",
            "hint": "The websites and feeds to crawl.",
            "status": _plural(sources, "source"),
            "done": bool(sources),
            "action": "Add a source",
            "url": reverse("admin:sources_source_add"),
        },
        {
            "title": "Run a crawl",
            "hint": "Press Run now on a source and watch it live.",
            "status": _plural(runs, "run"),
            "done": bool(runs),
            "action": "Go to Run now",
            "url": reverse("crawler:dashboard") + "#crawl-sources",
        },
        {
            "title": "Raw articles",
            "hint": "Check what was stored and which articles matched a keyword.",
            "status": f"{stored:,} stored, {matched:,} matched",
            "done": bool(stored),
            "action": "View articles",
            "url": articles_url,
        },
        {
            "title": "LLM processing",
            "hint": "Automatic with Redis and the LLM worker running, or use Process with LLM "
                    "on an article.",
            "status": llm_status,
            "done": bool(processed) and not waiting,
            "action": "View matched articles",
            "url": f"{articles_url}?is_keyword_matched__exact=1",
        },
        {
            "title": "Results",
            "hint": "Review the summaries and extracted disease events.",
            "status": _plural(events, "event"),
            "done": bool(events),
            "action": "View events",
            "url": reverse("admin:llm_extractedevent_changelist"),
        },
    ]
    for number, step in enumerate(steps, start=1):
        step["number"] = number
        step["next"] = False
    first_unfinished = next((step for step in steps if not step["done"]), None)
    if first_unfinished:
        first_unfinished["next"] = True
    return steps


@register.inclusion_tag("core/workflow.html")
def workflow() -> dict:
    steps = build_steps()
    return {"steps": steps, "next_step": next((s for s in steps if s["next"]), None)}
