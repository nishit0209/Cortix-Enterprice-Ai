"""TOTP MFA on top of django-otp (devices are global, keyed by user)."""

from __future__ import annotations

from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

DEVICE_NAME = "default"


def start_setup(user) -> TOTPDevice:
    with transaction.atomic():
        TOTPDevice.objects.filter(user=user, confirmed=False).delete()
        return TOTPDevice.objects.create(user=user, name=DEVICE_NAME, confirmed=False)


def confirm_setup(user, code: str) -> TOTPDevice | None:
    device = TOTPDevice.objects.filter(user=user, confirmed=False).order_by("-id").first()
    if device is None or not device.verify_token(code):
        return None
    with transaction.atomic():
        TOTPDevice.objects.filter(user=user, confirmed=True).delete()
        device.confirmed = True
        device.save(update_fields=["confirmed"])
        user.mfa_enabled = True
        user.save(update_fields=["mfa_enabled", "updated_at"])
    return device


def verify_login_token(user, code: str) -> TOTPDevice | None:
    for device in TOTPDevice.objects.filter(user=user, confirmed=True):
        if device.verify_token(code):  # drift tolerance, replay protection and throttling by django-otp
            return device
    return None


def disable(user) -> None:
    with transaction.atomic():
        TOTPDevice.objects.filter(user=user).delete()
        user.mfa_enabled = False
        user.save(update_fields=["mfa_enabled", "updated_at"])
