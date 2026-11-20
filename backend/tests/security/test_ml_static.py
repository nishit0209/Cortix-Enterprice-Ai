"""Static ML safety checks over the `apps/ml` source (no database; CONTRACT "W4 additions: Safety rules for ML").

  * No ML view accepts file uploads: no `request.FILES`, no `MultiPartParser` / `FileUploadParser` / `FormParser`
    (models are trained by Nexora itself; uploaded or user-supplied pickles are never loaded).
  * Deserialisation happens only next to an integrity check: every `joblib.load` / `pickle.load(s)` call sits in a
    function (or, failing that, a module) that also computes / compares a sha256.
  * No other unsafe deserialisers at all: `dill`, `cloudpickle`, `torch.load`, `numpy.load(..., allow_pickle=True)`,
    `pandas.read_pickle`, `marshal.loads`, `shelve`.
  * Only `apps/ai` may import `google.genai` (AGENT_GUIDE rule 5); `apps/ml` reaches Gemini through `GeminiService`.

Each check is vacuous until `apps/ml` has real code, so the module skips while `apps/ml` is still the W0 stub.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ML_DIR = Path(__file__).resolve().parents[2] / "apps" / "ml"


def _sources() -> list[Path]:
    return sorted(
        p
        for p in ML_DIR.rglob("*.py")
        if "tests" not in p.relative_to(ML_DIR).parts and "migrations" not in p.relative_to(ML_DIR).parts
    )


def _is_stub() -> bool:
    return sum(p.stat().st_size for p in _sources()) < 2000


needs_ml_code = pytest.mark.skipif(_is_stub(), reason="apps/ml is still the W0 stub (AI/ML W4 builds it)")


def _trees():
    for path in _sources():
        yield path, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _rel(path: Path) -> str:
    return path.relative_to(ML_DIR.parents[1]).as_posix()


@needs_ml_code
def test_no_ml_code_reads_uploaded_files():
    bad = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            name = _dotted(node) if isinstance(node, ast.Attribute | ast.Name) else ""
            if name.endswith(".FILES") or name.split(".")[-1] in {"MultiPartParser", "FileUploadParser", "FormParser"}:
                bad.append(f"{_rel(path)}:{node.lineno}: {name}")
    assert bad == [], "ML routes must not accept uploads (no user-supplied pickles):\n" + "\n".join(sorted(set(bad)))


UNSAFE_CALLS = {
    "dill.load", "dill.loads", "cloudpickle.load", "cloudpickle.loads", "torch.load", "pd.read_pickle",
    "pandas.read_pickle", "marshal.loads", "marshal.load", "shelve.open",
}
UNSAFE_IMPORTS = {"dill", "cloudpickle", "torch", "shelve", "marshal"}
LOADERS = {"joblib.load", "pickle.load", "pickle.loads", "load", "loads"}


@needs_ml_code
def test_no_unsafe_deserialisers_in_ml():
    bad = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                bad += [f"{_rel(path)}:{node.lineno}: import {m}" for m in mods if m.split(".")[0] in UNSAFE_IMPORTS]
            if isinstance(node, ast.Call):
                name = _dotted(node.func)
                if name in UNSAFE_CALLS:
                    bad.append(f"{_rel(path)}:{node.lineno}: {name}()")
                if name.endswith("np.load") or name.endswith("numpy.load"):
                    for kw in node.keywords:
                        if kw.arg == "allow_pickle" and not (isinstance(kw.value, ast.Constant) and not kw.value.value):
                            bad.append(f"{_rel(path)}:{node.lineno}: {name}(allow_pickle=...)")
    assert bad == [], "\n".join(bad)


def _imported_loader_names(tree: ast.Module) -> set[str]:
    """Local names bound to joblib.load / pickle.load(s) by `from joblib import load [as x]`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"joblib", "pickle"}:
            for alias in node.names:
                if alias.name in {"load", "loads"}:
                    names.add(alias.asname or alias.name)
    return names


@needs_ml_code
def test_every_artifact_deserialisation_is_next_to_a_sha256_check():
    unchecked, seen = [], 0
    for path, tree in _trees():
        source = path.read_text(encoding="utf-8")
        local_loaders = _imported_loader_names(tree)
        funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _dotted(node.func)
            if name not in {"joblib.load", "pickle.load", "pickle.loads"} and name not in local_loaders:
                continue
            seen += 1
            enclosing = [f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)]
            scope = ast.get_source_segment(source, enclosing[-1]) if enclosing else source
            if "sha256" not in (scope or "") and "sha256" not in source:
                unchecked.append(f"{_rel(path)}:{node.lineno}: {name}() with no sha256 verification nearby")
    assert unchecked == [], "\n".join(unchecked)
    if seen == 0:
        pytest.skip("apps/ml deserialises no artifact yet")


@needs_ml_code
def test_ml_never_imports_the_gemini_sdk():
    bad = []
    for path, tree in _trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Import | ast.ImportFrom):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                bad += [f"{_rel(path)}:{node.lineno}: {m}" for m in mods if m.startswith(("google.genai", "google"))]
    assert bad == [], "apps/ml must reach Gemini only through apps.ai GeminiService:\n" + "\n".join(bad)


# ------------------------------------------------------------------------------------------------ self-test
_BAD = """
import joblib
import dill
from pickle import loads as unpickle
from google import genai
from rest_framework.parsers import MultiPartParser


class Upload:
    parser_classes = [MultiPartParser]

    def post(self, request):
        return joblib.load(request.FILES["model"])


def unsafe(blob):
    import numpy as np
    np.load(blob, allow_pickle=True)
    return unpickle(blob)
"""

_GOOD = """
import hashlib
import io

import joblib


def load_artifact(data: bytes, expected_sha256: str):
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise ValueError("artifact integrity")
    return joblib.load(io.BytesIO(data))
"""


@pytest.mark.parametrize(
    "check",
    [
        test_no_ml_code_reads_uploaded_files,
        test_no_unsafe_deserialisers_in_ml,
        test_every_artifact_deserialisation_is_next_to_a_sha256_check,
        test_ml_never_imports_the_gemini_sdk,
    ],
    ids=lambda f: f.__name__,
)
def test_the_static_checks_catch_unsafe_code_and_accept_safe_code(tmp_path, monkeypatch, check):
    import sys

    module = sys.modules[__name__]
    bad_root = tmp_path / "bad" / "apps" / "ml"
    good_root = tmp_path / "good" / "apps" / "ml"
    for root, src in ((bad_root, _BAD), (good_root, _GOOD)):
        root.mkdir(parents=True)
        (root / "artifacts.py").write_text(src, encoding="utf-8")
    monkeypatch.setattr(module, "ML_DIR", bad_root)
    with pytest.raises(AssertionError):
        check()
    monkeypatch.setattr(module, "ML_DIR", good_root)
    try:
        check()
    except pytest.skip.Exception:
        pass
