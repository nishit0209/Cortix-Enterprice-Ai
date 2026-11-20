"""Regression tests (no database) for the wave-1 core defects reported by QA."""

from __future__ import annotations

from apps.core.tenancy.operations import EnableTenantRLS, MakeAppendOnly


class _RecordingEditor:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql, params=()):
        self.calls.append((str(sql), params))


def test_migration_operations_execute_sql_verbatim():
    """SQL containing a literal '%' must reach the schema editor with params=None (no client-side merging)."""
    for op in (MakeAppendOnly("t_probe"), EnableTenantRLS("t_probe"), EnableTenantRLS("t_probe", column="id")):
        for direction in ("database_forwards", "database_backwards"):
            editor = _RecordingEditor()
            getattr(op, direction)("core", editor, None, None)
            assert editor.calls
            assert all(params is None for _, params in editor.calls), (op, direction)
    editor = _RecordingEditor()
    MakeAppendOnly("t_probe").database_forwards("core", editor, None, None)
    assert "%" in editor.calls[0][0]  # the RAISE placeholder is still there, executed verbatim


def test_tenant_task_resets_actor_after_each_run(monkeypatch):
    from contextlib import contextmanager
    from uuid import uuid4

    from apps.core.tenancy import tasks as tasks_mod
    from apps.core.tenancy.context import current_actor, current_org

    @contextmanager
    def fake_context(org_id):
        token = current_org.set(org_id)
        try:
            yield
        finally:
            current_org.reset(token)

    monkeypatch.setattr(tasks_mod, "tenant_context", fake_context)
    monkeypatch.setattr(tasks_mod, "_assert_org_active", lambda: None)

    seen = []

    class Probe(tasks_mod.TenantTask):
        name = "core.tests.probe"

        def run(self, **kwargs):
            seen.append(current_actor.get())
            if kwargs.get("boom"):
                raise RuntimeError("boom")
            return "ok"

    task = Probe()
    user_id = uuid4()
    assert current_actor.get() == ("system", None)
    assert task(org_id=str(uuid4()), actor_id=str(user_id)) == "ok"
    assert current_actor.get() == ("system", None)
    try:
        task(org_id=str(uuid4()), actor_id=str(user_id), boom=True)
    except RuntimeError:
        pass
    assert current_actor.get() == ("system", None)
    task(org_id=str(uuid4()))  # no actor: runs as system even if a previous task had a user
    assert seen == [("user", user_id), ("user", user_id), ("system", None)]
    Probe.long_running = True
    task(org_id=str(uuid4()), actor_id=str(user_id))
    assert seen[-1] == ("user", user_id) and current_actor.get() == ("system", None)


def test_finalize_response_rolls_back_only_its_own_tenant_transaction(monkeypatch):
    from types import SimpleNamespace

    from rest_framework.response import Response
    from rest_framework.test import APIRequestFactory
    from rest_framework.views import APIView

    from apps.core.tenancy import views as views_mod

    calls = []
    monkeypatch.setattr(views_mod.transaction, "set_rollback", lambda flag, using=None: calls.append(flag))
    # an outer caller/test transaction is active
    monkeypatch.setattr(views_mod, "connection", SimpleNamespace(in_atomic_block=True))

    class Probe(views_mod.TenantScopedMixin, APIView):
        pass

    request = APIView().initialize_request(APIRequestFactory().get("/x"))
    view = Probe()
    view.headers = {}
    view.args, view.kwargs, view.request = (), {}, request
    view.format_kwarg = None
    view._tenant_atomic_entered = False  # never entered (anonymous / non-member 404)
    view.finalize_response(request, Response(status=404))
    assert calls == []
    view._tenant_atomic_entered = True
    view.finalize_response(request, Response(status=200))
    assert calls == []
    view.finalize_response(request, Response(status=400))
    assert calls == [True]


def test_storage_keys_reject_traversal_and_malformed_parts():
    import uuid

    import pytest

    from apps.core import storage
    from apps.core.tenancy.context import current_org

    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    token = current_org.set(org_a)
    try:
        storage._check_key(f"orgs/{org_a}/uploads/x/f.csv")
        assert storage.org_key("uploads/x", "f.csv") == f"orgs/{org_a}/uploads/x/f.csv"
        for bad in (
            f"orgs/{org_a}/../{org_b}/f",
            f"orgs/{org_a}/./f",
            f"orgs/{org_a}//f",
            f"orgs/{org_a}/",
            f"orgs/{org_a}/dir/",
            f"orgs/{org_a}/a" + chr(92) + ".." + chr(92) + "b",  # backslash traversal
            f"orgs/{org_a}/a\nb",
            f"/orgs/{org_a}/f",
        ):
            with pytest.raises(PermissionError):
                storage._check_key(bad)
        for parts in (("..", str(org_b), "f"), ("a/../b",), ("",), (".",), ("a" + chr(92) + "b",), ()):
            with pytest.raises(ValueError):
                storage.org_key(*parts)
    finally:
        current_org.reset(token)


def test_content_disposition_is_rfc6266_and_injection_free():
    from urllib.parse import unquote

    from apps.core.http import content_disposition

    value = content_disposition("evil\"; filename*=UTF-8''x.html\r\nX-Test: 1")
    assert "\r" not in value and "\n" not in value
    assert value.count('"') == 2
    assert "filename*=UTF-8''x.html" not in value
    assert value.startswith('attachment; filename="')
    assert content_disposition("report.csv") == "attachment; filename=\"report.csv\"; filename*=UTF-8''report.csv"
    uni = content_disposition("बिक्री रिपोर्ट.xlsx")
    fallback, ext = uni.split("; filename*=UTF-8''")
    assert fallback.encode("ascii") and unquote(ext) == "बिक्री रिपोर्ट.xlsx"
    assert content_disposition("../../etc/passwd").startswith('attachment; filename="_.._etc_passwd"')
    assert content_disposition("\r\n") == "attachment; filename=\"download\"; filename*=UTF-8''download"


def test_reauth_failed_problem_marks_the_password_field():
    from types import SimpleNamespace

    import pytest

    from apps.core.auth import require_password
    from apps.core.exceptions import ReauthFailed, problem_exception_handler

    user = SimpleNamespace(check_password=lambda raw: raw == "right")
    require_password(user, "right")
    for bad in ("wrong", "", None):
        with pytest.raises(ReauthFailed):
            require_password(user, bad)
    resp = problem_exception_handler(ReauthFailed(), {})
    assert resp.status_code == 400
    assert resp.data["code"] == "reauth_failed"
    assert resp.data["errors"] == [{"field": "password", "code": "reauth_failed", "message": "The password is incorrect."}]
