"""Follow management + minimal auth (register, token login)."""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response

from apps.tweets.models import TwitterUser

from .models import Follow
from .serializers import FollowSerializer


def _normalize_handle(raw: str) -> str:
    return (raw or "").strip().lstrip("@").lower()


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.ALLOW_REGISTRATION:
            return Response({"detail": "registration disabled"}, status=403)
        username = (request.data.get("username") or "").strip()
        password = request.data.get("password") or ""
        if not username or not password:
            return Response({"detail": "username and password required"}, status=400)
        if User.objects.filter(username=username).exists():
            return Response({"detail": "username taken"}, status=400)
        user = User.objects.create_user(username=username, password=password)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key}, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        user = authenticate(
            username=request.data.get("username"), password=request.data.get("password")
        )
        if user is None:
            return Response({"detail": "invalid credentials"}, status=400)
        token, _ = Token.objects.get_or_create(user=user)
        return Response({"token": token.key})


class FollowView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        follows = Follow.objects.filter(user=request.user).select_related("account")
        return Response(FollowSerializer(follows, many=True).data)

    def post(self, request):
        handle = _normalize_handle(request.data.get("handle") or "")
        if not handle:
            return Response({"detail": "handle required"}, status=400)
        account, created = TwitterUser.objects.get_or_create(handle=handle)
        need_fetch = created or not account.tracking
        if need_fetch:
            account.tracking = True
            account.quarantined = False
            account.quarantine_reason = ""
            account.quarantined_at = None
            account.save(
                update_fields=[
                    "tracking",
                    "quarantined",
                    "quarantine_reason",
                    "quarantined_at",
                ]
            )
            from apps.fetching.tasks import fetch_account_historical, fetch_account_live

            fetch_account_historical.delay(handle)
            fetch_account_live.delay(handle)
        Follow.objects.get_or_create(user=request.user, account=account)
        return Response(
            FollowSerializer(Follow.objects.get(user=request.user, account=account)).data,
            status=status.HTTP_201_CREATED,
        )

    def delete(self, request):
        handle = _normalize_handle(request.data.get("handle") or "")
        Follow.objects.filter(user=request.user, account__handle=handle).delete()
        account = TwitterUser.objects.filter(handle=handle).first()
        if account is not None and not account.followers.exists():
            account.tracking = False
            account.save(update_fields=["tracking"])
        return Response(status=status.HTTP_204_NO_CONTENT)
