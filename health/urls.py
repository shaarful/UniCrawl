from django.urls import path

from . import views

urlpatterns = [
    path("", views.liveness, name="health-liveness"),
    path("ready/", views.readiness, name="health-readiness"),
]
