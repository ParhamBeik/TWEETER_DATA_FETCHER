"""Registration, login, refresh, logout, and who-am-I.

JWT rather than DRF's TokenAuthentication: those tokens never expire, so a
leaked one stays valid until somebody notices and deletes the row. Access tokens
here last 30 minutes, refresh tokens rotate on use, and the one just used is
blacklisted -- a replayed refresh token is dead on arrival.

New accounts are ordinary non-staff users. They can read the archive; operating
the fetcher and replacing the shared X session require staff (see
tweets.permissions).
"""
from __future__ import annotations

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenRefreshView

MAX_USERNAME_LENGTH = 150


def issue_tokens(user) -> dict:
    """The token pair plus the identity the UI needs to render itself."""
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
        "user": user_payload(user),
    }


def user_payload(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        # Drives which controls the console offers. The API enforces this
        # independently -- this field is a hint for the UI, never the gate.
        "is_staff": user.is_staff,
    }


class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if not settings.ALLOW_REGISTRATION:
            return Response({"detail": "Registration is closed."}, status=403)

        username = str(request.data.get("username") or "").strip()
        email = str(request.data.get("email") or "").strip()
        password = request.data.get("password") or ""

        errors: dict[str, list[str]] = {}
        if not username:
            errors["username"] = ["Choose a username."]
        elif len(username) > MAX_USERNAME_LENGTH:
            errors["username"] = [f"Keep it under {MAX_USERNAME_LENGTH} characters."]
        elif User.objects.filter(username__iexact=username).exists():
            # iexact, so "Parham" and "parham" cannot both exist and confuse a
            # login attempt later.
            errors["username"] = ["That username is taken."]

        if email:
            try:
                validate_email(email)
            except ValidationError:
                errors["email"] = ["Enter a valid email address."]
            else:
                if User.objects.filter(email__iexact=email).exists():
                    errors["email"] = ["That email is already registered."]

        if not password:
            errors["password"] = ["Choose a password."]
        else:
            # Validated against the real Django validators, not a mirror of the
            # frontend's rules -- the client-side checks are a courtesy, this is
            # the decision.
            candidate = User(username=username, email=email)
            try:
                validate_password(password, user=candidate)
            except ValidationError as exc:
                errors["password"] = list(exc.messages)

        if errors:
            return Response({"detail": _first_message(errors), "errors": errors}, status=400)

        try:
            with transaction.atomic():
                user = User.objects.create_user(
                    username=username, email=email, password=password
                )
        except IntegrityError:
            # Two simultaneous signups for one username: the unique constraint
            # is the real arbiter, the check above is just the friendly path.
            return Response(
                {"detail": "That username is taken.", "errors": {"username": ["That username is taken."]}},
                status=400,
            )

        return Response(issue_tokens(user), status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    # The one endpoint where an unlimited number of attempts is a real attack.
    # Rate limiting rather than account lockout: a lockout is a denial of
    # service anyone can inflict on you by failing on purpose with your username.
    throttle_scope = "login"

    def post(self, request):
        raw_username = str(request.data.get("username") or "").strip()
        matched_user = User.objects.filter(username__iexact=raw_username).first()
        username = matched_user.username if matched_user is not None else raw_username
        user = authenticate(username=username, password=request.data.get("password") or "")
        if user is None or not user.is_active:
            # One message for both "no such user" and "wrong password", so the
            # endpoint cannot be used to enumerate accounts.
            return Response({"detail": "Incorrect username or password."}, status=400)
        return Response(issue_tokens(user))


class RefreshView(TokenRefreshView):
    """SimpleJWT's refresh, with this project's error shape.

    ROTATE_REFRESH_TOKENS + BLACKLIST_AFTER_ROTATION mean the base view already
    issues a new refresh token and blacklists the spent one; all that is added
    here is a `detail` message the frontend's error handling understands.
    """

    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        try:
            return super().post(request, *args, **kwargs)
        except (TokenError, InvalidToken):
            return Response({"detail": "Session expired — please sign in again."}, status=401)


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        raw = request.data.get("refresh") or ""
        if raw:
            try:
                RefreshToken(raw).blacklist()
            except TokenError:
                # Already expired or already blacklisted. Logging out is
                # idempotent -- the caller wanted the token dead, and it is.
                pass
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    def get(self, request):
        return Response(user_payload(request.user))


def _first_message(errors: dict[str, list[str]]) -> str:
    for messages in errors.values():
        if messages:
            return messages[0]
    return "Invalid request."
