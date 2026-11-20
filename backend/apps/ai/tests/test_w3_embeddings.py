"""Local embedding model availability (no download, no network): not-downloaded / offline / cooldown behaviour,
`manage.py download_models` and the `ai.reembed_pending` periodic job."""

from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from uuid import uuid4

import numpy as np
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.ai import embeddings, reembed
from apps.ai.embeddings import (
    EmbeddingUnavailable,
    FakeEmbedder,
    LocalEmbedder,
    UnavailableEmbedder,
    embed_query,
    embedder_status,
    embedding_available,
    override_embedder,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class StubST:
    """Stands in for sentence_transformers.SentenceTransformer (records how it was constructed)."""

    created: list[dict] = []
    fail_offline = False

    def __init__(self, name, cache_folder=None, device=None, local_files_only=None):
        if StubST.fail_offline and not local_files_only:
            raise OSError("We couldn't connect to 'https://huggingface.co' (offline)")
        StubST.created.append({"name": name, "cache_folder": cache_folder, "local_files_only": local_files_only})

    def get_sentence_embedding_dimension(self):
        return 4

    def encode(self, texts, **kw):
        return np.array([[1.0, 0.0, 0.0, 0.0]] * len(texts))


@pytest.fixture
def st(monkeypatch):
    StubST.created = []
    StubST.fail_offline = False
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=StubST))
    return StubST


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """huggingface_hub.snapshot_download(local_files_only=True) answering from a fake cache."""
    state = {"cached": False, "calls": 0}

    def snapshot_download(repo_id, cache_dir=None, local_files_only=False):
        state["calls"] += 1
        assert local_files_only is True  # availability checks never download
        if not state["cached"]:
            raise FileNotFoundError("LocalEntryNotFoundError")
        path = tmp_path / "snap"
        path.mkdir(exist_ok=True)
        return str(path)

    monkeypatch.setitem(sys.modules, "huggingface_hub", types.SimpleNamespace(snapshot_download=snapshot_download))
    return state


def test_not_downloaded_model_is_unavailable_without_loading(st, hub):
    clock = Clock()
    emb = LocalEmbedder("intfloat/multilingual-e5-base", dim=4, clock=clock)
    assert emb.available is False and emb.is_cached() is False
    with pytest.raises(EmbeddingUnavailable) as err:
        emb.embed_queries(["revenue"])
    assert err.value.reason == "model_not_downloaded"
    assert st.created == []  # never constructed -> no download attempt
    status = emb.status()
    assert status["available"] is False and status["cached"] is False and "download_models" in status["last_error"]
    with pytest.raises(EmbeddingUnavailable) as again:  # cooldown: fails fast without even checking the cache
        emb.embed_queries(["revenue"])
    assert again.value.reason == "model_unavailable"


def test_cached_model_loads_local_files_only(st, hub):
    hub["cached"] = True
    emb = LocalEmbedder("intfloat/multilingual-e5-base", dim=4, clock=Clock())
    assert emb.available is True
    vec = emb.embed_queries(["revenue"])
    assert vec.shape == (1, 4)
    assert st.created == [{"name": "intfloat/multilingual-e5-base", "cache_folder": emb.cache_dir,
                           "local_files_only": True}]


def test_load_failure_is_reported_and_retried_after_cooldown(st, hub, monkeypatch):
    hub["cached"] = True
    clock = Clock()

    class Broken(StubST):
        def __init__(self, *a, **k):
            raise RuntimeError("corrupt cache")

    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Broken))
    emb = LocalEmbedder("m", dim=4, clock=clock)
    with pytest.raises(EmbeddingUnavailable) as err:
        emb.embed_passages(["x"])
    assert err.value.reason == "load_failed" and emb.available is False
    monkeypatch.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=StubST))
    clock.t += embeddings.FAILURE_COOLDOWN_S + 1
    assert emb.available is True
    assert emb.embed_passages(["x"]).shape == (1, 4)


def test_download_fetches_then_reports_the_path(st, hub):
    emb = LocalEmbedder("intfloat/multilingual-e5-base", dim=4, allow_download=False, clock=Clock())

    def fake_ctor(name, cache_folder=None, device=None, local_files_only=None):
        hub["cached"] = True  # the "download" populates the cache
        return StubST(name, cache_folder, device, local_files_only)

    sys.modules["sentence_transformers"] = types.SimpleNamespace(SentenceTransformer=fake_ctor)
    path = emb.download()
    assert path and path.endswith("snap") and emb.loaded
    assert st.created[-1]["local_files_only"] is False


def test_offline_download_fails_cleanly(st, hub):
    st.fail_offline = True
    emb = LocalEmbedder("m", dim=4, clock=Clock())
    with pytest.raises(EmbeddingUnavailable) as err:
        emb.download()
    assert err.value.reason == "load_failed" and "offline" in str(err.value)


def test_missing_library_is_unavailable(hub, monkeypatch):
    hub["cached"] = True
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)  # import raises ImportError
    with pytest.raises(EmbeddingUnavailable) as err:
        LocalEmbedder("m", dim=4, clock=Clock()).embed_queries(["x"])
    assert err.value.reason == "library_missing"


def test_module_helpers_with_overrides():
    with override_embedder(UnavailableEmbedder(dim=8)):
        assert embedding_available() is False
        assert embedder_status()["available"] is False
        with pytest.raises(EmbeddingUnavailable):
            embed_query("x")
    with override_embedder(FakeEmbedder(dim=8)):
        assert embedding_available() is True and embedder_status()["provider"] == "fake"
        assert len(embed_query("x")) == 8


def test_model_cache_dir_setting(settings, monkeypatch):
    monkeypatch.delenv("SENTENCE_TRANSFORMERS_HOME", raising=False)
    monkeypatch.setenv("HF_HOME", "/tmp/hf")
    settings.EMBEDDING_CACHE_DIR = ""
    assert embeddings.model_cache_dir() == "/tmp/hf"
    settings.EMBEDDING_CACHE_DIR = "/models"
    assert embeddings.model_cache_dir() == "/models"


# ------------------------------------------------------------------------------------- download_models
@pytest.fixture
def command_embedder(monkeypatch):
    from apps.ai.management.commands import download_models

    holder = {}

    def make(model):
        emb = holder.get("emb")
        return emb

    monkeypatch.setattr(download_models.Command, "make_embedder", staticmethod(make))
    return holder


class CmdEmbedder:
    def __init__(self, *, ok=True, cached=True):
        self.model_name, self.dim, self.cache_dir = "intfloat/multilingual-e5-base", 768, "C:/hf"
        self.ok, self.cached = ok, cached

    def download(self):
        if not self.ok:
            raise EmbeddingUnavailable("could not load: offline", reason="load_failed")
        return "C:/hf/models--intfloat--multilingual-e5-base/snapshots/abc"

    def cached_path(self):
        return "C:/hf/snap" if self.cached else None

    def embed_queries(self, texts):
        return np.zeros((len(texts), 768), dtype=np.float32)


def test_download_models_success_json(command_embedder, capsys):
    command_embedder["emb"] = CmdEmbedder()
    call_command("download_models", "--json")
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready" and report["vector_dim"] == 768
    assert report["path"].endswith("snapshots/abc") and report["cache_dir"] == "C:/hf"


def test_download_models_failure_exit_code(command_embedder, capsys):
    command_embedder["emb"] = CmdEmbedder(ok=False)
    with pytest.raises(CommandError) as err:
        call_command("download_models")
    assert err.value.returncode == 1
    out = capsys.readouterr().out
    assert "failed" in out and "full-text" in out


def test_download_models_check_only(command_embedder, capsys):
    command_embedder["emb"] = CmdEmbedder(cached=True)
    call_command("download_models", "--check", "--json")
    assert json.loads(capsys.readouterr().out)["status"] == "cached"
    command_embedder["emb"] = CmdEmbedder(cached=False)
    with pytest.raises(CommandError):
        call_command("download_models", "--check")


def test_download_models_real_embedder_is_download_enabled():
    from apps.ai.management.commands.download_models import Command

    emb = Command.make_embedder("some/model")
    assert isinstance(emb, LocalEmbedder) and emb.allow_download and emb.model_name == "some/model"


# ---------------------------------------------------------------------------------------- reembed job
@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(reembed, "_registry", {})
    calls = []

    @contextmanager
    def fake_tenant_context(org_id):
        calls.append(("ctx", org_id))
        yield

    import apps.core.tenancy as tenancy

    monkeypatch.setattr(tenancy, "tenant_context", fake_tenant_context)
    return calls


def _reembedder(name, orgs, calls, *, done=3, raise_exc=None):
    def run(emb, limit):
        calls.append((name, limit))
        if raise_exc:
            raise raise_exc
        return done

    reembed.register_reembedder(reembed.Reembedder(name, f"SELECT DISTINCT org_id FROM {name}", run))
    return orgs


def test_reembed_nothing_pending_never_touches_the_model(registry, monkeypatch):
    _reembedder("t1", [], registry)
    monkeypatch.setattr(reembed, "scan_orgs", lambda r, using="ops": [])
    with override_embedder(UnavailableEmbedder()):
        assert reembed.run_reembed_pending() == {"pending": 0}
    assert registry == []


def test_reembed_runs_each_org_in_its_tenant_context(registry, monkeypatch):
    o1, o2 = uuid4(), uuid4()
    _reembedder("documents.chunks", [o1, o2], registry, done=5)
    _reembedder("nl2sql.cache", [o2], registry, done=1)
    scans = {"documents.chunks": [o1, o2], "nl2sql.cache": [o2]}
    monkeypatch.setattr(reembed, "scan_orgs", lambda r, using="ops": scans[r.name])
    with override_embedder(FakeEmbedder(dim=8)):
        out = reembed.run_reembed_pending(batch=100)
    assert out == {"documents.chunks": 10, "nl2sql.cache": 1}
    assert registry == [("ctx", o1), ("documents.chunks", 100), ("ctx", o2), ("documents.chunks", 100),
                        ("ctx", o2), ("nl2sql.cache", 100)]


def test_reembed_skips_when_model_unavailable_and_download_disabled(registry, monkeypatch):
    _reembedder("t", [uuid4()], registry)
    monkeypatch.setattr(reembed, "scan_orgs", lambda r, using="ops": [uuid4()])
    with override_embedder(UnavailableEmbedder()):
        out = reembed.run_reembed_pending(allow_download=False)
    assert out == {"skipped": "model_unavailable", "pending_orgs": 1}
    assert [c for c in registry if c[0] != "ctx"] == []


def test_reembed_downloads_in_the_background_when_allowed(registry, monkeypatch):
    _reembedder("t", [uuid4()], registry, done=2)
    monkeypatch.setattr(reembed, "scan_orgs", lambda r, using="ops": [uuid4()])

    class Downloadable(LocalEmbedder):
        def __init__(self):
            super().__init__("m", dim=4)
            self.downloaded = False

        @property
        def available(self):
            return self.downloaded

        def download(self):
            self.downloaded = True
            return "path"

    emb = Downloadable()
    with override_embedder(emb):
        assert reembed.run_reembed_pending(allow_download=True) == {"t": 2}
    assert emb.downloaded


def test_reembed_one_failing_org_does_not_stop_the_others(registry, monkeypatch):
    o1, o2 = uuid4(), uuid4()
    calls = registry

    def run(emb, limit):
        calls.append(("run", limit))
        if len([c for c in calls if c[0] == "run"]) == 1:
            raise RuntimeError("boom")
        return 4

    reembed.register_reembedder(reembed.Reembedder("t", "SELECT 1", run))
    monkeypatch.setattr(reembed, "scan_orgs", lambda r, using="ops": [o1, o2])
    with override_embedder(FakeEmbedder(dim=4)):
        assert reembed.run_reembed_pending() == {"t": 4}


def test_reembed_stops_when_the_model_disappears(registry, monkeypatch):
    _reembedder("t", [uuid4()], registry, raise_exc=EmbeddingUnavailable("gone"))
    monkeypatch.setattr(reembed, "scan_orgs", lambda r, using="ops": [uuid4(), uuid4()])
    with override_embedder(FakeEmbedder(dim=4)):
        out = reembed.run_reembed_pending()
    assert out == {"t": 0, "stopped": "model_unavailable"}


def test_periodic_registration_is_skipped_without_scheduler(monkeypatch):
    monkeypatch.setitem(sys.modules, "apps.core.scheduler", None)
    assert reembed.register_periodic_job() is False
    seen = []
    monkeypatch.setitem(sys.modules, "apps.core.scheduler", types.SimpleNamespace(
        register_periodic=lambda name, every, fn: seen.append((name, every, fn))))
    assert reembed.register_periodic_job() is True
    assert seen == [("ai.reembed_pending", 600, reembed.run_reembed_pending)]


def test_apps_register_their_reembedders():
    from django.apps import apps as django_apps

    assert django_apps.is_installed("apps.documents") and django_apps.is_installed("apps.nl2sql")
    names = {r.name for r in reembed.registered()}
    assert {"documents.chunks", "nl2sql.cache"} <= names
    sql = {r.name: r.scan_sql for r in reembed.registered()}
    assert "embedding_pending" in sql["documents.chunks"] and "embedding IS NULL" in sql["nl2sql.cache"]
