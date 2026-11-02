from django.urls import path

from . import views

app_name = "llm"

urlpatterns = [
    path("articles/<int:article_id>/process/", views.article_process, name="article-process"),
]
