from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AccountTimelineView, FeedView, SearchViewSet

router = DefaultRouter()
router.register("searches", SearchViewSet, basename="search")

urlpatterns = [
    path("feed/", FeedView.as_view(), name="feed"),
    path("accounts/<str:handle>/tweets/", AccountTimelineView.as_view(), name="account-timeline"),
]

urlpatterns += router.urls
