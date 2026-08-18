from django.urls import path

from .views import AccountsAnalyticsView, NarrativesView, OverviewView, TopicsView, VelocityView

urlpatterns = [
    path("stats/overview/", OverviewView.as_view(), name="stats-overview"),
    path("analytics/velocity/", VelocityView.as_view(), name="analytics-velocity"),
    path("analytics/topics/", TopicsView.as_view(), name="analytics-topics"),
    path("analytics/accounts/", AccountsAnalyticsView.as_view(), name="analytics-accounts"),
    path("analytics/narratives/", NarrativesView.as_view(), name="analytics-narratives"),
]
