"""`manage.py create_platform_admin --email <email> [--revoke]`

Sets `User.is_platform_admin` on an EXISTING user (sign up first, or `seed_demo`), which unlocks the platform
console (`/api/v1/platform/...`, the web app's Platform pages). `--revoke` clears the flag. Every change is
recorded in the append-only PlatformAuditEvent table. Running it again is a no-op.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.platform_admin import services


class Command(BaseCommand):
    help = "Grant (or with --revoke, remove) platform super-admin rights for an existing user."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Email of an existing user.")
        parser.add_argument("--revoke", action="store_true", help="Remove the platform-admin flag instead.")

    def handle(self, *args, email: str, revoke: bool = False, **options):
        try:
            user, changed = services.grant_platform_admin(email, revoke=revoke)
        except LookupError as exc:
            raise CommandError(f"No user with email {email!r}. Create the account first (sign up or seed_demo).") from exc
        state = "is no longer" if revoke else "is now"
        if changed:
            self.stdout.write(self.style.SUCCESS(f"{user.email} {state} a platform admin."))
        else:
            self.stdout.write(f"{user.email} {state} a platform admin (unchanged).")
