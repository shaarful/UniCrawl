"""Staff action: send one stored article to the LLM now and show the result."""
from __future__ import annotations

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ImproperlyConfigured
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from llm.gemini import ArticleError, ServiceError
from llm.models import ArticleExtraction
from llm.services import process_article
from raw_content.models import RawArticle


def _summary(extraction: ArticleExtraction) -> str:
    if not extraction.is_disease_event:
        outcome = "not a disease event"
    else:
        events = [str(event) for event in extraction.events.all()]
        outcome = f"{len(events)} event(s): {'; '.join(events)}" if events else "no events found"
    translated = "; translated to English" if extraction.translated_body else ""
    return f"Processed by the LLM: {outcome}{translated}."


@staff_member_required
@require_POST
def article_process(request, article_id: int):
    """Extract one article (article admin page / run page button)."""
    article = get_object_or_404(RawArticle, pk=article_id)
    try:
        ok, message = True, _summary(process_article(article))
    except (ArticleError, ServiceError, ImproperlyConfigured, OSError, ValueError) as exc:
        # Any earlier result stays in place.
        ok, message = False, f"LLM processing failed: {exc}"

    if request.headers.get("accept", "").startswith("application/json"):
        return JsonResponse({"ok": ok, "message": message})
    (messages.success if ok else messages.warning)(request, message)
    next_url = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        next_url = reverse("admin:raw_content_rawarticle_change", args=[article_id])
    return redirect(next_url)
