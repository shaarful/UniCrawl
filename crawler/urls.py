from django.urls import path

from . import views

app_name = "crawler"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("status.json", views.dashboard_status, name="dashboard-status"),
    path("sources/eios/add/", views.eios_board_add, name="eios-board-add"),
    path("sources/<int:source_id>/run/", views.start, name="start"),
    path("runs/<int:run_id>/", views.run_detail, name="run"),
    path("runs/<int:run_id>/status.json", views.run_status, name="run-status"),
    path("runs/<int:run_id>/stop/", views.run_stop, name="run-stop"),
    path("articles/<int:article_id>/refetch/", views.article_refetch, name="article-refetch"),
]
