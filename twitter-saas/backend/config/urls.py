from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("apps.tweets.urls")),
    path("api/", include("apps.accounts.urls")),
    path("api/", include("apps.analytics.urls")),
]
