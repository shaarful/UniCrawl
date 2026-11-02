"""Crawl dashboard: start crawls and watch their progress live (staff only).

The pages render a shell; the browser polls the *_status JSON endpoints about
once a second while a crawl is running.
"""
from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from crawler.forms import EIOSBoardForm
from crawler.models import CrawlRun
from crawler.refetch import refetch_article
from crawler.runs import AlreadyRunning, mark_stale_runs, request_stop, start_run
from raw_content.models import RawArticle
from sources.models import Source

FEED_SIZE = 25
RECENT_RUNS = 15


def _iso(dt):
    return dt.isoformat() if dt else None


def _run_json(run: CrawlRun) -> dict:
    return {
        "id": run.pk,
        "url": reverse("crawler:run", args=[run.pk]),
        "source": {"id": run.source_id, "name": run.source.name},
        "trigger": run.trigger,
        "status": run.status,
        "status_label": run.get_status_display(),
        "stage": run.stage,
        "percent": run.percent,
        "processed": run.processed,
        "new": run.new,
        "matched": run.matched,
        "published": run.published,
        "duplicates": run.duplicates,
        "errors": run.errors,
        "skipped": run.skipped,
        "full_text": run.full_text,
        "is_eios": run.source.content_type == Source.ContentType.EIOS,
        "error": run.error,
        "events": run.events or [],
        "cancel_requested": run.cancel_requested,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "heartbeat_at": _iso(run.heartbeat_at),
    }


def _article_json(article: RawArticle) -> dict:
    return {
        "id": article.pk,
        "title": article.title,
        "url": article.url,
        "status": article.processing_status,
        "language": article.language,
        "diseases": article.matched_diseases or [],
        "body_source": (article.metadata or {}).get("body_source", ""),
        "link_error": (article.metadata or {}).get("link_error", ""),
        "body_chars": len(article.body),
        "admin_url": reverse("admin:raw_content_rawarticle_change", args=[article.pk]),
        "refetch_url": reverse("crawler:article-refetch", args=[article.pk]),
        "llm_url": reverse("llm:article-process", args=[article.pk]),
    }


def _render_dashboard(request, eios_form: EIOSBoardForm, status: int = 200):
    return render(request, "crawler/dashboard.html", {
        "eios_form": eios_form,
        "eios_configured": bool(settings.EIOS_CLIENT_ID and settings.EIOS_TOKEN_URL),
    }, status=status)


@staff_member_required
def dashboard(request):
    return _render_dashboard(request, EIOSBoardForm())


@staff_member_required
@require_POST
def eios_board_add(request):
    """Add a WHO-EIOS board as a new source (dashboard form)."""
    form = EIOSBoardForm(request.POST)
    if not form.is_valid():
        return _render_dashboard(request, form, status=400)
    source = form.save(user=request.user)
    messages.success(request, f"Added {source.name}. Use “Run now” to test-crawl it.")
    return redirect("crawler:dashboard")


@staff_member_required
def dashboard_status(request):
    mark_stale_runs()
    sources = Source.objects.annotate(
        article_count=Count("raw_articles"),
        matched_count=Count("raw_articles", filter=Q(raw_articles__is_keyword_matched=True)),
    )
    runs = list(CrawlRun.objects.select_related("source")[:RECENT_RUNS])
    latest: dict[int, CrawlRun] = {}
    for run in CrawlRun.objects.select_related("source").filter(
        status=CrawlRun.Status.RUNNING,
    ):
        latest[run.source_id] = run
    for source in sources:
        if source.pk not in latest:
            run = source.crawl_runs.select_related("source").first()
            if run is not None:
                latest[source.pk] = run

    return JsonResponse({
        "sources": [
            {
                "id": s.pk,
                "name": s.name,
                "url": s.url,
                "content_type": s.get_content_type_display(),
                "is_active": s.is_active,
                "is_unavailable": s.is_temporarily_unavailable,
                "last_crawled_at": _iso(s.last_crawled_at),
                "next_crawl_at": _iso(s.next_crawl_at),
                "article_count": s.article_count,
                "matched_count": s.matched_count,
                "start_url": reverse("crawler:start", args=[s.pk]),
                "admin_url": reverse("admin:sources_source_change", args=[s.pk]),
                "run": _run_json(latest[s.pk]) if s.pk in latest else None,
            }
            for s in sources
        ],
        "runs": [_run_json(run) for run in runs],
    })


@staff_member_required
@require_POST
def start(request, source_id: int):
    source = get_object_or_404(Source, pk=source_id)
    try:
        run = start_run(source.pk, user=request.user)
    except AlreadyRunning as exc:
        messages.info(request, str(exc))
        return redirect("crawler:run", run_id=exc.run.pk)
    return redirect("crawler:run", run_id=run.pk)


@staff_member_required
def run_detail(request, run_id: int):
    run = get_object_or_404(CrawlRun.objects.select_related("source"), pk=run_id)
    return render(request, "crawler/run.html", {"run": run})


@staff_member_required
def run_status(request, run_id: int):
    mark_stale_runs()
    run = get_object_or_404(CrawlRun.objects.select_related("source"), pk=run_id)
    articles = RawArticle.objects.none()
    if run.fetched_at:
        articles = RawArticle.objects.filter(
            source_id=run.source_id,
            created_at__gte=run.started_at,
            fetched_at=run.fetched_at,
        ).order_by("-id")
    return JsonResponse({
        "run": _run_json(run),
        "latest": [_article_json(a) for a in articles[:FEED_SIZE]],
        "matched": [_article_json(a) for a in articles.filter(is_keyword_matched=True)[:FEED_SIZE]],
    })


@staff_member_required
@require_POST
def article_refetch(request, article_id: int):
    """Refetch one article from its original link (admin page / run page button)."""
    get_object_or_404(RawArticle, pk=article_id)
    result = refetch_article(article_id)
    if request.headers.get("accept", "").startswith("application/json"):
        return JsonResponse({"ok": result.ok, "message": result.message})
    (messages.success if result.ok else messages.warning)(request, result.message)
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        next_url = reverse("admin:raw_content_rawarticle_change", args=[article_id])
    return redirect(next_url)


@staff_member_required
@require_POST
def run_stop(request, run_id: int):
    run = get_object_or_404(CrawlRun, pk=run_id)
    request_stop(run)
    if request.headers.get("accept", "").startswith("application/json"):
        return JsonResponse({"ok": True})
    return redirect("crawler:run", run_id=run.pk)
