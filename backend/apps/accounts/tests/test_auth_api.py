"""API tests: /auth/*, /me, password change, TOTP MFA. Runs as nexora_app (RLS enforced)."""

import pytest
from django.conf import settings
from django_otp.oath import totp
from django_otp.plugins.otp_totp.models import TOTPDevice
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.tenancy import tenant_context
from apps.core.tests.fixtures import DEFAULT_PASSWORD, client_for, make_user, unique_email
from apps.orgs.models import Organization

pytestmark = pytest.mark.django_db

SIGNUP = "/api/v1/auth/signup"
LOGIN = "/api/v1/auth/login"


def signup_body(**over):
    return {"email": unique_email("signup"), "password": DEFAULT_PASSWORD, "full_name": "Sam Synthetic",
            "org_name": "Synthetic Signup Co", **over}


# --- csrf -------------------------------------------------------------------------------------------------
def test_csrf_endpoint_sets_cookie(anon_client):
    r = anon_client.get("/api/v1/auth/csrf")
    assert r.status_code == 204
    assert settings.CSRF_COOKIE_NAME in r.cookies


# --- signup -----------------------------------------------------------------------------------------------
@pytest.mark.ac("P2-AC3")
def test_signup_creates_user_org_and_session(anon_client):
    body = signup_body(ai_data_policy="full")  # must be ignored
    r = anon_client.post(SIGNUP, body, format="json")
    assert r.status_code == 201, r.content
    data = r.json()
    assert data["user"]["email"] == body["email"]
    assert data["user"]["full_name"] == "Sam Synthetic"
    org = data["org"]
    assert org["ai_data_policy"] == "synthetic_only"
    assert org["role"] == "owner"
    assert "org.ai_policy.consent" in org["permissions"]
    assert set(org) >= {"id", "slug", "name", "plan", "status", "region", "ai_data_policy", "created_at", "role",
                        "permissions"}
    with tenant_context(org["id"]):
        assert Organization.objects.get().ai_data_policy == "synthetic_only"
    me = anon_client.get("/api/v1/me")  # logged in by the signup
    assert me.status_code == 200
    assert [o["slug"] for o in me.json()["orgs"]] == [org["slug"]]


def test_signup_duplicate_email_is_case_insensitive(anon_client):
    make_user(email="dup.user@example.test")
    r = anon_client.post(SIGNUP, signup_body(email="Dup.User@Example.test"), format="json")
    assert r.status_code == 409
    assert r.json()["code"] == "email_taken"


def test_signup_rejects_weak_password(anon_client):
    r = anon_client.post(SIGNUP, signup_body(password="password"), format="json")
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "invalid"
    assert any(e["field"] == "password" for e in body["errors"])
    assert r["Content-Type"].startswith("application/problem+json")


@pytest.mark.parametrize("missing", ["email", "password", "full_name", "org_name"])
def test_signup_requires_fields(anon_client, missing):
    body = signup_body()
    body.pop(missing)
    r = anon_client.post(SIGNUP, body, format="json")
    assert r.status_code == 400
    assert any(e["field"] == missing for e in r.json()["errors"])


def test_signup_enforces_csrf():
    client = APIClient(enforce_csrf_checks=True)
    r = client.post(SIGNUP, signup_body(), format="json")
    assert r.status_code == 403
    assert r.json()["code"] == "csrf_failed"
    client.get("/api/v1/auth/csrf")
    token = client.cookies[settings.CSRF_COOKIE_NAME].value
    r = client.post(SIGNUP, signup_body(), format="json", HTTP_X_CSRFTOKEN=token)
    assert r.status_code == 201


# --- login / logout -----------------------------------------------------------------------------------------
def test_login_success_rotates_session(anon_client):
    user = make_user(email="login.ok@example.test")
    session = anon_client.session
    session["pre_login"] = True
    session.save()
    anon_client.cookies[settings.SESSION_COOKIE_NAME] = session.session_key
    r = anon_client.post(LOGIN, {"email": "LOGIN.OK@example.test", "password": DEFAULT_PASSWORD}, format="json")
    assert r.status_code == 200, r.content
    assert r.json()["user"]["id"] == str(user.id)
    new_key = r.cookies[settings.SESSION_COOKIE_NAME].value
    assert new_key and new_key != session.session_key
    assert anon_client.get("/api/v1/me").status_code == 200


def test_login_errors_are_generic(anon_client):
    make_user(email="known@example.test")
    wrong = anon_client.post(LOGIN, {"email": "known@example.test", "password": "Wrong-password-123"}, format="json")
    unknown = anon_client.post(LOGIN, {"email": "nobody@example.test", "password": "Wrong-password-123"}, format="json")
    assert wrong.status_code == unknown.status_code == 400
    assert wrong.json()["code"] == unknown.json()["code"] == "invalid_credentials"
    assert wrong.json()["detail"] == unknown.json()["detail"]


def test_login_inactive_user_rejected(anon_client):
    make_user(email="inactive@example.test", is_active=False)
    r = anon_client.post(LOGIN, {"email": "inactive@example.test", "password": DEFAULT_PASSWORD}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "invalid_credentials"


def test_login_lockout_after_repeated_failures(anon_client):
    make_user(email="brute@example.test")
    codes = [anon_client.post(LOGIN, {"email": "brute@example.test", "password": f"Wrong-{i}-password"},
                              format="json").status_code for i in range(5)]
    assert codes == [400, 400, 400, 400, 429]
    r = anon_client.post(LOGIN, {"email": "brute@example.test", "password": DEFAULT_PASSWORD}, format="json")
    assert r.status_code == 429
    assert r.json()["code"] == "login_locked"
    assert int(r["Retry-After"]) > 0


def test_login_validation_error(anon_client):
    r = anon_client.post(LOGIN, {"email": "not-an-email"}, format="json")
    assert r.status_code == 400
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"email", "password"} <= fields


def test_logout_ends_session(owner):
    client = client_for(owner)
    assert client.get("/api/v1/me").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert client.get("/api/v1/me").status_code == 401


def test_logout_anonymous_is_noop(anon_client):
    assert anon_client.post("/api/v1/auth/logout").status_code == 204


# --- me ---------------------------------------------------------------------------------------------------
def test_me_requires_authentication(anon_client):
    r = anon_client.get("/api/v1/me")
    assert r.status_code == 401
    assert r.json()["code"] == "not_authenticated"


def test_me_returns_user_and_orgs(owner, org, other_org):
    r = client_for(owner).get("/api/v1/me")
    assert r.status_code == 200
    data = r.json()
    assert set(data["user"]) >= {"id", "email", "full_name", "is_platform_admin", "timezone"}
    assert data["orgs"] == [{"id": str(org.id), "slug": org.slug, "name": org.name, "role": "owner",
                             "status": "active"}]


def test_me_patch(owner):
    client = client_for(owner)
    r = client.patch("/api/v1/me", {"full_name": "Olivia Renamed", "timezone": "Europe/Berlin"}, format="json")
    assert r.status_code == 200
    assert r.json()["user"]["full_name"] == "Olivia Renamed"
    assert r.json()["user"]["timezone"] == "Europe/Berlin"
    owner.refresh_from_db()
    assert owner.timezone == "Europe/Berlin"


def test_me_patch_rejects_unknown_timezone(owner):
    r = client_for(owner).patch("/api/v1/me", {"timezone": "Mars/Olympus_Mons"}, format="json")
    assert r.status_code == 400
    assert r.json()["errors"][0]["field"] == "timezone"


def test_me_rejects_api_keys(owner, org):
    from apps.accounts.services import create_api_key

    with tenant_context(org.id):
        _, token = create_api_key(name="ci", scopes=["org.view"], created_by=owner)
    r = APIClient().get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert r.status_code == 401


# --- password change ------------------------------------------------------------------------------------------
def test_password_change_flow(owner):
    client = client_for(owner)
    url = "/api/v1/auth/password/change"
    bad = client.post(url, {"current_password": "nope-nope-nope", "new_password": "An0ther-Synthetic-Pass!"},
                      format="json")
    assert bad.status_code == 400 and bad.json()["errors"][0]["field"] == "current_password"
    weak = client.post(url, {"current_password": DEFAULT_PASSWORD, "new_password": "12345678"}, format="json")
    assert weak.status_code == 400 and weak.json()["errors"][0]["field"] == "new_password"
    same = client.post(url, {"current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD}, format="json")
    assert same.status_code == 400
    ok = client.post(url, {"current_password": DEFAULT_PASSWORD, "new_password": "An0ther-Synthetic-Pass!"},
                     format="json")
    assert ok.status_code == 204
    assert client.get("/api/v1/me").status_code == 200  # session kept
    owner.refresh_from_db()
    assert owner.check_password("An0ther-Synthetic-Pass!")
    anon = APIClient()
    assert anon.post(LOGIN, {"email": owner.email, "password": DEFAULT_PASSWORD}, format="json").status_code == 400
    assert anon.post(LOGIN, {"email": owner.email, "password": "An0ther-Synthetic-Pass!"},
                     format="json").status_code == 200


def test_password_change_requires_auth(anon_client):
    r = anon_client.post("/api/v1/auth/password/change", {"current_password": "x", "new_password": "y"},
                         format="json")
    assert r.status_code == 401


# --- TOTP MFA ---------------------------------------------------------------------------------------------------
def _code(device, drift=0) -> str:
    return f"{totp(device.bin_key, step=device.step, t0=device.t0, digits=device.digits, drift=drift):06d}"


def test_mfa_setup_verify_login_and_disable(settings):
    settings.OTP_TOTP_THROTTLE_FACTOR = 0
    user = make_user(email="mfa.user@example.test")
    client = client_for(user)
    r = client.post("/api/v1/auth/mfa/totp/setup")
    assert r.status_code == 201
    assert r.json()["otpauth_url"].startswith("otpauth://totp/")
    device = TOTPDevice.objects.get(user=user, confirmed=False)

    assert client.post("/api/v1/auth/mfa/totp/verify", {"code": "000000" if _code(device) != "000000" else "111111"},
                       format="json").json()["code"] == "mfa_invalid"
    r = client.post("/api/v1/auth/mfa/totp/verify", {"code": _code(device)}, format="json")
    assert r.status_code == 200 and r.json()["user"]["mfa_enabled"] is True
    user.refresh_from_db()
    assert user.mfa_enabled

    anon = APIClient()
    r = anon.post(LOGIN, {"email": user.email, "password": DEFAULT_PASSWORD}, format="json")
    assert r.status_code == 400 and r.json()["code"] == "mfa_required" and r.json()["mfa_required"] is True
    device.refresh_from_db()
    r = anon.post(LOGIN, {"email": user.email, "password": DEFAULT_PASSWORD, "otp": _code(device, drift=1)},
                  format="json")
    assert r.status_code == 200, r.content

    assert client.post("/api/v1/auth/mfa/totp/setup").status_code == 409  # already enabled
    wrong_pw = client.post("/api/v1/auth/mfa/totp/disable", {"password": "Wrong-password-1", "code": "123456"},
                           format="json")
    assert wrong_pw.status_code == 400
    device.refresh_from_db()
    device.last_t = -1  # allow re-use of a time step inside this fast test
    device.save()
    r = client.post("/api/v1/auth/mfa/totp/disable", {"password": DEFAULT_PASSWORD, "code": _code(device)},
                    format="json")
    assert r.status_code == 200 and r.json()["user"]["mfa_enabled"] is False
    assert not TOTPDevice.objects.filter(user=user).exists()


def test_mfa_wrong_code_at_login_counts_as_failure():
    user = make_user(email="mfa.wrong@example.test", mfa_enabled=True)
    TOTPDevice.objects.create(user=user, name="default", confirmed=True)
    r = APIClient().post(LOGIN, {"email": user.email, "password": DEFAULT_PASSWORD, "otp": "999999"}, format="json")
    assert r.status_code == 400
    assert r.json()["code"] in ("mfa_invalid",)


def test_signup_org_name_used_for_slug(anon_client):
    r = anon_client.post(SIGNUP, signup_body(org_name="Zeta Synthetic Labs"), format="json")
    assert r.status_code == 201
    assert r.json()["org"]["slug"].startswith("zeta-synthetic-labs")
    assert User.objects.filter(email=r.json()["user"]["email"]).exists()
