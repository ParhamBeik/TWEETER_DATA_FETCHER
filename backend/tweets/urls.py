"""The whole /api/ surface: auth, feed, accounts, runs, searches, analytics."""
from django.urls import path
from rest_framework.routers import DefaultRouter

from .analytics import (
    AccountsAnalyticsView,
    NarrativesView,
    OverviewView,
    TopicsView,
    VelocityView,
)
from .auth_views import LoginView, LogoutView, MeView, RefreshView, RegisterView
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
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("auth/refresh/", RefreshView.as_view(), name="refresh"),
    path("auth/logout/", LogoutView.as_view(), name="logout"),
    path("auth/me/", MeView.as_view(), name="me"),
    path("feed/", FeedView.as_view(), name="feed"),
    path("export/", ExportView.as_view(), name="export"),
    path("runs/", FetchRunListView.as_view(), name="fetch-runs"),
    path("runs/<str:run_id>/", FetchRunDetailView.as_view(), name="fetch-run-detail"),
    path("cycles/", CycleView.as_view(), name="cycles"),
    path("session/", XSessionView.as_view(), name="x-session"),
    path("accounts/<str:handle>/tweets/", AccountTimelineView.as_view(), name="account-timeline"),
    path("stats/overview/", OverviewView.as_view(), name="stats-overview"),
    path("analytics/velocity/", VelocityView.as_view(), name="analytics-velocity"),
    path("analytics/topics/", TopicsView.as_view(), name="analytics-topics"),
    path("analytics/accounts/", AccountsAnalyticsView.as_view(), name="analytics-accounts"),
    path("analytics/narratives/", NarrativesView.as_view(), name="analytics-narratives"),
]

urlpatterns += router.urls
