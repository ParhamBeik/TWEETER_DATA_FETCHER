"""Delete every user account so the app can be re-registered from scratch.

Destructive and irreversible: it removes login accounts, their JWT blacklist
rows, and any legacy DRF tokens. It does NOT touch collected tweets, tracked
accounts, searches, or the X session -- those belong to the collector, not to
whoever is logged in.

    python manage.py reset_users            # show what would go
    python manage.py reset_users --yes      # do it
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Delete all user accounts. Requires --yes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true", help="Actually delete. Without it this is a dry run."
        )

    def handle(self, *args, **options):
        users = User.objects.all()
        total = users.count()
        if not total:
            self.stdout.write("No users to delete.")
            return

        for user in users.order_by("username"):
            role = "staff" if user.is_staff else "user"
            self.stdout.write(f"  {user.username} ({role})")

        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING(f"\n{total} account(s) would be deleted. Re-run with --yes.")
            )
            return

        with transaction.atomic():
            # Cascades take the tokens and blacklist rows with them.
            deleted, _ = users.delete()
        self.stdout.write(self.style.WARNING(f"Deleted {total} account(s) ({deleted} rows)."))
        self.stdout.write("Register the first account through the app, then promote it in /admin/.")
