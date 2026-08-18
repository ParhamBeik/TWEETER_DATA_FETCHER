from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (
    AccountTimelineView,
    AccountViewSet,
    CycleView,
    ExportView,
    FeedView,
    FetchRunDetailView,
    FetchRunListView,
    SearchViewSet,
    XSessionView,
)

router = DefaultRouter()
router.register("searches", SearchViewSet, basename="search")
router.register("accounts", AccountViewSet, basename="account")

urlpatterns = [
    path("feed/", FeedView.as_view(), name="feed"),
    path("export/", ExportView.as_view(), name="export"),
    path("runs/", FetchRunListView.as_view(), name="fetch-runs"),
    path("runs/<str:run_id>/", FetchRunDetailView.as_view(), name="fetch-run-detail"),
    path("cycles/", CycleView.as_view(), name="cycles"),
    path("session/", XSessionView.as_view(), name="x-session"),
    path("accounts/<str:handle>/tweets/", AccountTimelineView.as_view(), name="account-timeline"),
]

urlpatterns += router.urls
