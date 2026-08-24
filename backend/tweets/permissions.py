"""Who may operate the fetcher, as opposed to read what it collected.

Signup is open, and every authenticated user used to inherit the project-wide
IsAuthenticated -- which meant anyone who registered could POST a replacement X
session, retier accounts, or trigger fetch cycles against the one shared,
rate-limited operator session. Reading the archive is what a new account is for;
operating the collector is not.
"""
from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission


class IsStaffOrReadOnly(BasePermission):
    """Anyone signed in may read; only staff may write."""

    message = "This action requires an operator account."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not (user and user.is_authenticated):
            return False
        return request.method in SAFE_METHODS or bool(user.is_staff)


class IsStaff(BasePermission):
    """Staff only, reads included.

    For endpoints where even the read is privileged -- session health names the
    account the shared X session belongs to and when it was last refreshed.
    """

    message = "This action requires an operator account."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(user and user.is_authenticated and user.is_staff)
