"""W5 document intelligence (API_SPECIFICATION Phase 8): collections, schema extraction (local rules; Gemini only
through the policy gate) with review, local comparison, and table promotion to a dataset. Real DB with RLS."""

from __future__ import annotations

import io

import pytest

from apps.ai.policy import check_policy
from apps.ai.services import override_gemini
from apps.ai.testing import FakeTransport, build_engine
from apps.documents.models import Document, DocumentTable, ExtractionResult

from . import synth

pytestmark = pytest.mark.django_db

CONTRACT_V1 = (
    "# Service Agreement\n\nThis agreement is between Synthetic Buyer Pvt Ltd and Example Vendor Ltd.\n\n"
    "## Fees\n\nThe monthly fee is Rs. 50,000 payable within 30 days of the invoice.\n\n"
    "## Term\n\nThe term starts on 2026-01-01 and runs for twelve months.\n\n"
    "## Confidentiality\n\nBoth parties keep the synthetic information confidential.\n"
)
CONTRACT_V2 = (
    "# Service Agreement\n\nThis agreement is between Synthetic Buyer Pvt Ltd and Example Vendor Ltd.\n\n"
    "## Fees\n\nThe monthly fee is Rs. 65,000 payable within 45 days of the invoice.\n\n"
    "## Term\n\nThe term starts on 2026-01-01 and runs for twelve months.\n\n"
    "## Termination\n\nEither party may terminate with sixty days written notice.\n"
)


def _url(world, path: str) -> str:
    return f"/api/v1/orgs/{world.slug}/{path}"


@pytest.fixture
def no_ocr(monkeypatch):
    from apps.documents import extraction

    monkeypatch.setattr(extraction, "tesseract_languages", lambda: None)


@pytest.fixture
def upload(api_client_for, memory_storage, fake_embedder, no_ocr, django_capture_on_commit_callbacks):
    def _upload(world, name: str, data: bytes, **fields) -> Document:
        f = io.BytesIO(data)
        f.name = name
        client = api_client_for(world.owner)
        with django_capture_on_commit_callbacks(execute=True):
            resp = client.post(_url(world, "documents"), {"file": f, **fields}, format="multipart")
        assert resp.status_code == 202, resp.content
        with world.tenant():
            doc = Document.objects.get(id=resp.json()["document"]["id"])
        assert doc.status == "ready", doc.error
        return doc

    return _upload


def test_collections_crud_assignment_and_permissions(two_orgs, upload, api_client_for):
    a = two_orgs.a
    owner, viewer = api_client_for(a.owner), api_client_for(a.viewer)
    resp = owner.post(_url(a, "document-collections"), {"name": "Supplier invoices FY26",
                                                        "classification": "internal"}, format="json")
    assert resp.status_code == 201, resp.content
    coll = resp.json()
    assert coll["document_count"] == 0
    assert owner.post(_url(a, "document-collections"), {"name": "Supplier invoices FY26"},
                      format="json").status_code == 400
    assert viewer.post(_url(a, "document-collections"), {"name": "x"}, format="json").status_code == 403

    doc = upload(a, "invoice.txt", synth.INVOICE_TEXT.encode(), collection_id=coll["id"])
    assert str(doc.collection_id) == coll["id"]
    other = upload(a, "report.txt", synth.txt_bytes())
    listed = viewer.get(_url(a, f"documents?collection_id={coll['id']}")).json()["items"]
    assert [d["id"] for d in listed] == [str(doc.id)] and listed[0]["collection_id"] == coll["id"]
    assert viewer.get(_url(a, "document-collections")).json()["items"][0]["document_count"] == 1

    patched = owner.patch(_url(a, f"documents/{other.id}"), {"collection_id": coll["id"]}, format="json")
    assert patched.status_code == 200 and patched.json()["collection_id"] == coll["id"]
    assert owner.patch(_url(a, f"document-collections/{coll['id']}"), {"name": "Invoices"},
                       format="json").json()["name"] == "Invoices"
    assert owner.patch(_url(a, f"documents/{other.id}"), {"collection_id": None}, format="json").json()[
        "collection_id"] is None
    assert owner.delete(_url(a, f"document-collections/{coll['id']}")).status_code == 204
    with a.tenant():
        assert Document.objects.get(id=doc.id).collection_id is None  # documents stay

    intruder = api_client_for(two_orgs.b.owner)
    assert intruder.get(_url(two_orgs.b, f"documents/{doc.id}")).status_code == 404


def test_local_invoice_extraction_cites_every_value_and_review(two_orgs, upload, api_client_for):
    a = two_orgs.a
    doc = upload(a, "invoice.txt", synth.INVOICE_TEXT.encode())
    owner = api_client_for(a.owner)
    assert api_client_for(a.viewer).post(_url(a, f"documents/{doc.id}/extract"), {"schema_key": "invoice"},
                                         format="json").status_code == 403
    assert owner.post(_url(a, f"documents/{doc.id}/extract"), {"schema_key": "passport"},
                      format="json").status_code == 400
    resp = owner.post(_url(a, f"documents/{doc.id}/extract"), {"schema_key": "invoice"}, format="json")
    assert resp.status_code == 201, resp.content
    ext = resp.json()
    assert ext["status"] == "needs_review" and ext["method"] == "local"
    r = ext["result"]
    assert r["invoice_number"] == "INV-2026-0042" and r["invoice_date"] == "2026-03-15"
    assert r["due_date"] == "2026-04-14" and r["gstin"] == "27ABCDE1234F1Z5"
    assert r["total_amount"] == 141600.0 and r["subtotal"] == 120000.0 and r["currency"] == "INR"
    assert r["buyer_name"] == "Acme Synthetic Traders Pvt Ltd"
    with a.tenant():
        page_text = doc.pages.get(number=1).text
    for name, value in r.items():
        cites = ext["field_citations"][name]
        if value is None:
            assert cites is None, name  # uncited fields are null and vice versa
            continue
        c = cites[0]
        assert c["page"] == 1 and page_text[c["start"]:c["end"]].strip() == c["quote"], (name, c)
    assert ext["fields_found"] >= 9 and ext["fields_total"] == 12

    listed = api_client_for(a.viewer).get(_url(a, f"documents/{doc.id}/extractions")).json()["items"]
    assert [x["id"] for x in listed] == [ext["id"]]

    review = owner.post(_url(a, f"document-extractions/{ext['id']}/review"),
                        {"decision": "approve", "corrections": {"tax_amount": "21,600.00"}, "note": "checked"},
                        format="json")
    assert review.status_code == 200, review.content
    body = review.json()
    assert body["status"] == "approved" and body["result"]["tax_amount"] == 21600.0
    assert body["corrections"] == {"tax_amount": "21,600.00"} and body["reviewed_by"] == str(a.owner.pk)
    again = owner.post(_url(a, f"document-extractions/{ext['id']}/review"), {"decision": "reject"}, format="json")
    assert again.status_code == 400
    bad = owner.post(_url(a, f"documents/{doc.id}/extract"), {"schema_key": "invoice"}, format="json").json()
    assert owner.post(_url(a, f"document-extractions/{bad['id']}/review"),
                      {"decision": "approve", "corrections": {"nope": 1}}, format="json").status_code == 400

    from apps.core.models import AuditEvent

    with a.tenant():
        actions = set(AuditEvent.objects.filter(action__startswith="document.extract").values_list("action",
                                                                                                    flat=True))
    assert {"document.extract", "document.extraction.approved"} <= actions


def test_resume_and_receipt_schemas_local():
    from apps.documents.structured import extract_fields

    receipt = ("Synthetic Mart Pvt Ltd\nReceipt No: RCPT-88812\nDate: 03 Feb 2026\nGST: Rs. 62.00\n"
               "Total: Rs. 1,302.00\nPaid by UPI\n")
    r = extract_fields("receipt", [(1, receipt)]).result
    assert r == {"receipt_number": "RCPT-88812", "date": "2026-02-03", "merchant": "Synthetic Mart Pvt Ltd",
                 "tax_amount": 62.0, "total_amount": 1302.0, "payment_method": "upi", "currency": "INR"}
    cv = ("Asha Verma\nTitle: Senior Data Analyst\nEmail: asha.verma@example.test | Phone: +91 98765 43210\n"
          "linkedin.com/in/asha-verma-synthetic\n7+ years of analytics experience in retail.\n"
          "Skills: Python, SQL, Power BI; Machine Learning\nEducation\nB.Tech in Computer Science, 2017\n")
    out = extract_fields("resume", [(1, cv)])
    assert out.result["name"] == "Asha Verma" and out.result["years_experience"] == 7
    assert out.result["skills"] == ["Python", "SQL", "Power BI", "Machine Learning"]
    assert out.result["current_title"] == "Senior Data Analyst" and out.result["email"] == "asha.verma@example.test"
    assert out.result["education"][0].startswith("B.Tech")
    assert all(out.citations[k] for k, v in out.result.items() if v is not None)


def test_ai_extraction_is_policy_gated_and_quotes_are_verified(two_orgs, upload, api_client_for, settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    a = two_orgs.a
    owner = api_client_for(a.owner)
    internal = upload(a, "invoice.txt", synth.INVOICE_TEXT.encode())
    engine, parts = build_engine(policy_check=check_policy)
    with override_gemini(engine):
        body = owner.post(_url(a, f"documents/{internal.id}/extract"), {"schema_key": "invoice", "mode": "ai"},
                          format="json").json()
    assert body["method"] == "local" and body["reason"] == "ai_policy_blocked"
    assert parts["transport"].total_calls == 0  # nothing left the server
    assert body["result"]["invoice_number"] == "INV-2026-0042"

    synthetic = upload(a, "invoice2.txt", synth.INVOICE_TEXT.encode(), classification="synthetic", attest="true")
    fields = [{"name": "invoice_number", "value": "INV-2026-0042", "source_id": f"S{i}",
               "quote": "Invoice No: INV-2026-0042"} for i in range(1, 2)]
    fields += [{"name": "total_amount", "value": "₹ 9,99,999.00", "source_id": "S1", "quote": "Amount Due: 9,99,999"},
               {"name": "gstin", "value": "27ABCDE1234F1Z5", "source_id": "S2", "quote": "GSTIN: 27ABCDE1234F1Z5"},
               {"name": "email", "value": "billing@acme-synthetic.example", "source_id": "S1",
                "quote": "Contact: billing@acme-synthetic.example"}]
    transport = FakeTransport().push(FakeTransport.json({"fields": fields}))
    engine, _ = build_engine(transport=transport, policy_check=check_policy)
    with override_gemini(engine):
        body = owner.post(_url(a, f"documents/{synthetic.id}/extract"), {"schema_key": "invoice", "mode": "ai"},
                          format="json").json()
    assert body["method"] == "gemini" and transport.total_calls == 1, body
    assert body["result"]["invoice_number"] == "INV-2026-0042" and body["result"]["gstin"] == "27ABCDE1234F1Z5"
    assert body["result"]["total_amount"] is None, "an unverifiable quote (hallucinated amount) is dropped"
    assert body["field_citations"]["total_amount"] is None and body["status"] == "needs_review"
    assert body["result"]["email"] is None, "a value cited from the wrong source is dropped"
    assert body["field_citations"]["gstin"][0]["page"] == 1


def test_compare_sections_and_entities_locally(two_orgs, upload, api_client_for):
    a = two_orgs.a
    v1 = upload(a, "contract_v1.txt", CONTRACT_V1.encode())
    v2 = upload(a, "contract_v2.txt", CONTRACT_V2.encode())
    viewer = api_client_for(a.viewer)
    resp = viewer.post(_url(a, "documents/compare"), {"a": str(v1.id), "b": str(v2.id)}, format="json")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    by_title = {(s["title_a"] or s["title_b"]): s for s in body["sections"]}
    assert by_title["Fees"]["status"] == "changed"
    changes = " ".join(f"{c['a']} -> {c['b']}" for c in by_title["Fees"]["changes"])
    assert "50,000" in changes and "65,000" in changes
    assert by_title["Term"]["status"] == "unchanged"
    assert by_title["Confidentiality"]["status"] == "removed" and by_title["Termination"]["status"] == "added"
    assert body["counts"]["changed"] >= 1 and 0 < body["similarity"] < 1
    assert "INR 65000.00" in body["entity_changes"]["amount"]["added"]
    assert body["summary"] is None and body["reason"] is None
    assert viewer.post(_url(a, "documents/compare"), {"a": str(v1.id), "b": str(v1.id)},
                       format="json").status_code == 400
    from apps.core.tests.fixtures import member_with_permissions

    reader = api_client_for(member_with_permissions(a.org, {"org.view", "documents.view"}))
    assert reader.post(_url(a, "documents/compare"), {"a": str(v1.id), "b": str(v2.id)},
                       format="json").status_code == 200
    assert reader.post(_url(a, "documents/compare"), {"a": str(v1.id), "b": str(v2.id), "summary": True},
                       format="json").status_code == 403  # an AI summary needs documents.ask
    other = api_client_for(two_orgs.b.owner)
    assert other.post(_url(two_orgs.b, "documents/compare"), {"a": str(v1.id), "b": str(v2.id)},
                      format="json").status_code == 404


def test_compare_summary_is_policy_gated(two_orgs, upload, api_client_for, settings):
    settings.GEMINI_API_KEY = "test-key-NOT-REAL"
    a = two_orgs.a
    v1 = upload(a, "contract_v1.txt", CONTRACT_V1.encode())
    v2 = upload(a, "contract_v2.txt", CONTRACT_V2.encode())
    engine, parts = build_engine(policy_check=check_policy)
    with override_gemini(engine):
        body = api_client_for(a.owner).post(_url(a, "documents/compare"),
                                            {"a": str(v1.id), "b": str(v2.id), "summary": True}, format="json").json()
    assert body["summary"] is None and body["reason"] == "ai_policy_blocked" and body["sections"]
    assert parts["transport"].total_calls == 0


@pytest.mark.django_db(transaction=False)
def test_table_promotion_publishes_a_dataset(two_orgs, api_client_for, memory_storage, fake_embedder,
                                             django_capture_on_commit_callbacks):
    from apps.documents.tests.factories import make_document

    a = two_orgs.a
    with a.tenant():
        doc = make_document(title="Regional sales (synthetic)")
        table = DocumentTable.objects.create(document=doc, page=1, index=0, header=["Region", "Units", ""],
                                             rows=[["North", "120", "45000"], ["South", "95", "38000"],
                                                   ["East", "80"]], n_rows=3, n_cols=3)
    owner, viewer = api_client_for(a.owner), api_client_for(a.viewer)
    assert viewer.post(_url(a, f"document-tables/{table.id}/promote"), {}, format="json").status_code == 403
    assert owner.post(_url(a, f"document-tables/{table.id}/promote"), {"classification": "synthetic"},
                      format="json").status_code == 422  # attestation required
    with django_capture_on_commit_callbacks(execute=True):
        resp = owner.post(_url(a, f"document-tables/{table.id}/promote"), {"name": "Regional sales table"},
                          format="json")
    assert resp.status_code in (201, 202), resp.content
    out = resp.json()
    assert out["rows"] == 3 and out["columns"] == ["Region", "Units", "column_3"]
    detail = owner.get(_url(a, f"datasets/{out['dataset_id']}")).json()
    assert detail["name"] == "Regional sales table" and detail["status"] == "ready", detail
    assert detail["classification"] == "internal"
    assert viewer.get(_url(a, f"document-tables/{table.id}")).json()["promoted_dataset_id"] == out["dataset_id"]
    from apps.lineage.models import LineageEdge

    with a.tenant():
        assert LineageEdge.objects.filter(from_type="document", from_id=doc.id, to_type="dataset").exists()
    other = api_client_for(two_orgs.b.owner)
    assert other.post(_url(two_orgs.b, f"document-tables/{table.id}/promote"), {}, format="json").status_code == 404


def test_extraction_rows_are_tenant_isolated(two_orgs, upload, api_client_for):
    a, b = two_orgs
    doc = upload(a, "invoice.txt", synth.INVOICE_TEXT.encode())
    ext = api_client_for(a.owner).post(_url(a, f"documents/{doc.id}/extract"), {"schema_key": "invoice"},
                                       format="json").json()
    intruder = api_client_for(b.owner)
    assert intruder.get(_url(b, f"document-extractions/{ext['id']}")).status_code == 404
    assert intruder.post(_url(b, f"document-extractions/{ext['id']}/review"), {"decision": "approve"},
                         format="json").status_code == 404
    with b.tenant():
        assert not ExtractionResult.objects.filter(id=ext["id"]).exists()
