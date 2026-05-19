"""Top-level URL configuration.

API endpoints are versioned at /api/v1/ (SRS §2.5 constraint).
"""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/", include("health.urls")),
    path("api/v1/sources/", include("sources.urls")),
]
