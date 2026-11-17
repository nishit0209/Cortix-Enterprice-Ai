"""Public/auth routes mounted at /api/v1/ (auth/*, me)."""

from django.urls import path

from .views import (
    CsrfView,
    LoginView,
    LogoutView,
    MeView,
    PasswordChangeView,
    SignupView,
    TotpDisableView,
    TotpSetupView,
    TotpVerifyView,
)

urlpatterns = [
    path("auth/csrf", CsrfView.as_view(), name="auth-csrf"),
    path("auth/signup", SignupView.as_view(), name="auth-signup"),
    path("auth/login", LoginView.as_view(), name="auth-login"),
    path("auth/logout", LogoutView.as_view(), name="auth-logout"),
    path("auth/password/change", PasswordChangeView.as_view(), name="auth-password-change"),
    path("auth/mfa/totp/setup", TotpSetupView.as_view(), name="auth-mfa-totp-setup"),
    path("auth/mfa/totp/verify", TotpVerifyView.as_view(), name="auth-mfa-totp-verify"),
    path("auth/mfa/totp/disable", TotpDisableView.as_view(), name="auth-mfa-totp-disable"),
    path("me", MeView.as_view(), name="me"),
]
