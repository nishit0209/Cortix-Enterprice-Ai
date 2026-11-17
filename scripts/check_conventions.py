"""Repository convention checks (CI-blocking).

1. The tenant setting is spelled 'app.org_id' ONLY in apps/core/tenancy/constants.py.
2. `.unscoped` (cross-tenant manager) only in platform modules.
3. `google.genai` / `from google import genai` only in backend/apps/ai/.
4. No other LLM provider SDKs in requirements.
5. No raw `.raw(` / `.extra(` ORM calls.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
errors: list[str] = []


def py_files():
    for p in BACKEND.rglob("*.py"):
        parts = set(p.parts)
        if ".venv" in parts or "node_modules" in parts:
            continue
        yield p


GUC_HOME = BACKEND / "apps" / "core" / "tenancy" / "constants.py"
GUC_RE = re.compile(r"""['"]app\.[a-z_]+['"]""")
UNSCOPED_ALLOWED = ("apps/platform_admin/", "apps/core/tenancy/", "apps/core/ops")
GENAI_RE = re.compile(r"^\s*(from\s+google\s+import\s+genai|import\s+google\.genai|from\s+google\.genai)", re.M)
BANNED_SDKS = re.compile(r"^(openai|anthropic|groq|mistralai|cohere|google-generativeai)\b", re.I | re.M)
RAW_RE = re.compile(r"\.objects\.(raw|extra)\(")

for p in py_files():
    rel = p.relative_to(BACKEND).as_posix()
    text = p.read_text(encoding="utf-8", errors="ignore")
    if p != GUC_HOME and GUC_RE.search(text):
        errors.append(f"{rel}: tenant setting literal found — import TENANT_GUC from apps.core.tenancy.constants")
    if ".unscoped" in text and not rel.startswith(UNSCOPED_ALLOWED) and "/tests/" not in rel and not rel.startswith("tests/"):
        errors.append(f"{rel}: `.unscoped` manager used outside platform modules")
    if GENAI_RE.search(text) and not rel.startswith("apps/ai/"):
        errors.append(f"{rel}: google.genai imported outside apps/ai (Gemini access must go through apps.ai)")
    if RAW_RE.search(text):
        errors.append(f"{rel}: ORM .raw()/.extra() is banned")

for req in BACKEND.glob("requirements*.txt"):
    if BANNED_SDKS.search(req.read_text(encoding="utf-8")):
        errors.append(f"{req.name}: banned LLM SDK in requirements (Gemini is the only provider)")

if errors:
    print("Convention violations:")
    print("\n".join(f"  - {e}" for e in errors))
    sys.exit(1)
print("conventions OK")
