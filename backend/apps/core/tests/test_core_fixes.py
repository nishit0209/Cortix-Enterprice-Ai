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
