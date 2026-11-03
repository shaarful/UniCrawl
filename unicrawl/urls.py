"""Top-level URL configuration.

API endpoints are versioned at /api/v1/ (SRS §2.5 constraint).
"""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

admin.site.login_template = "crawler/login.html"
admin.site.site_header = "RZDO EWS"
admin.site.site_title = "RZDO EWS"
admin.site.index_title = "Administration"
admin.site.site_url = None  # the header links to the crawl dashboard instead

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="crawler:dashboard", permanent=False)),
    path("crawl/", include("crawler.urls")),
    path("llm/", include("llm.urls")),
    path("admin/", admin.site.urls),
    path("health/", include("health.urls")),
    path("api/v1/sources/", include("sources.urls")),
]
