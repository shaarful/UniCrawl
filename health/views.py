"""Liveness / readiness probes (SRS §4.4.1)."""
from __future__ import annotations

from django.http import JsonResponse


def liveness(request) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def readiness(request) -> JsonResponse:
    # TODO: check DB + Redis reachability once those are wired up.
    return JsonResponse({"status": "ok", "checks": {}})
