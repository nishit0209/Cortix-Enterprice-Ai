"""Export formula injection (SECURITY_ARCHITECTURE §7): in CSV/XLSX exports every cell starting with
= + - @ TAB CR is neutralised (prefixed with ') and no XLSX cell is a formula. Values are not lost.

Flow (public APIs only): ingest testdata/generated/adversarial/formula_injection.csv as a synthetic dataset ->
BI conversation -> builder question "number of rows by note" (the payload cells become group keys) ->
POST /exports {message_id, format}. Skips while the BI/exports APIs are not merged.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from testing.client import api_client_for, requires_tenant_route, route_exists
from testing.flows import upload_and_ingest
from testing.security import FORMULA_TRIGGERS, is_dangerous_cell, unescaped_formula_cells
from testing.sse import read_bytes

pytestmark = [pytest.mark.django_db, requires_tenant_route("exports", "Backend bi")]
ADV = Path(__file__).resolve().parents[3] / "testdata" / "generated" / "adversarial"


def _payload_notes() -> list[str]:
    raw = (ADV / "formula_injection.csv").read_text(encoding="utf-8")
    return [r["note"] for r in csv.DictReader(io.StringIO(raw))]


@pytest.fixture
def exported(two_orgs, memory_storage, fake_redis, fake_embedder, gated_fake_gemini,
             django_capture_on_commit_callbacks):
    a = two_orgs.a
    base = f"/api/v1/orgs/{a.slug}"
    if not (route_exists(f"{base}/bi/conversations") and route_exists(f"{base}/exports")):
        pytest.skip("BI conversations / exports not implemented yet (Backend bi)")
    gated_fake_gemini.respond("*", stream=["Counts per note."])
    client = api_client_for(a.owner)
    ds = upload_and_ingest(client, a.slug, "formula_injection.csv", (ADV / "formula_injection.csv").read_bytes(),
                           django_capture_on_commit_callbacks, name="Formula probe")
    conv = client.post(f"{base}/bi/conversations", {"dataset_id": ds["id"]}, format="json")
    assert conv.status_code == 201, conv.content[:300]
    msg = client.post(f"{base}/bi/conversations/{conv.json()['id']}/messages", {"question": "number of rows by note"},
                      format="json", HTTP_ACCEPT="application/json")
    assert msg.status_code == 200, msg.content[:300]
    message = msg.json()
    assert message["result"] and message["result"]["rows"], message

    def export(fmt: str) -> bytes:
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(f"{base}/exports", {"message_id": message["id"], "format": fmt}, format="json")
        assert resp.status_code in (200, 201, 202), resp.content[:300]
        if resp.status_code == 200 and not resp.get("Content-Type", "").startswith("application/json"):
            return read_bytes(resp)  # synchronous file download
        export_id = resp.json()["id"]
        detail = client.get(f"{base}/exports/{export_id}")
        assert detail.status_code == 200, detail.content[:300]
        keys = [k for k in memory_storage.keys_for(a.id) if "export" in k]
        assert keys, "export finished but no file was stored"
        return memory_storage.objects[keys[-1]]

    export.message = message
    return export


@pytest.mark.ac("P4-AC3")
def test_csv_export_neutralises_formula_cells(exported):
    text = exported("csv").decode("utf-8-sig")
    assert unescaped_formula_cells(text) == [], unescaped_formula_cells(text)
    cells = {c for row in csv.reader(io.StringIO(text)) for c in row}
    for note in _payload_notes():
        if is_dangerous_cell(note) and not note.lstrip("-").replace(".", "", 1).isdigit():
            assert "'" + note in cells, f"{note!r} missing or not escaped in the CSV export"


@pytest.mark.ac("P4-AC3")
def test_xlsx_export_has_no_formulas_and_escapes_triggers(exported):
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(exported("xlsx")), data_only=False)
    bad = []
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    bad.append((cell.coordinate, cell.value, "formula"))
                elif isinstance(cell.value, str) and cell.value.startswith(FORMULA_TRIGGERS):
                    if not getattr(cell, "quotePrefix", False) and not cell.value.startswith("'"):
                        bad.append((cell.coordinate, cell.value, "unescaped"))
    assert bad == [], bad


def test_chat_result_keeps_the_raw_values(exported):
    """Escaping is an export concern: the stored/displayed result keeps the exact cell text."""
    keys = {str(r[0]) for r in exported.message["result"]["rows"]}
    assert {n for n in _payload_notes() if n.startswith("=")} <= keys
