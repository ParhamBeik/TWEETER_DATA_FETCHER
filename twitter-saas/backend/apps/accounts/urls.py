from django.urls import path

from .views import FollowView, LoginView, RegisterView

urlpatterns = [
    path("auth/register/", RegisterView.as_view(), name="register"),
    path("auth/login/", LoginView.as_view(), name="login"),
    path("follows/", FollowView.as_view(), name="follows"),
]
