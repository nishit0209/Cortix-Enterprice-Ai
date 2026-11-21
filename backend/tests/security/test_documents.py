"""Document Intelligence security (SECURITY_ARCHITECTURE §7, AI_ARCHITECTURE §5; API_SPECIFICATION Phase 8).

Uploads: zip-bomb DOCX, wrong magic bytes, oversized files, XXE inside a DOCX and a 2,001-page PDF are refused
(4xx, or an accepted upload whose processing ends rejected/failed) — never a 500, never stored when refused,
never an expanded external entity.
Q&A: under the default `synthetic_only` policy no text of an `internal` document may reach Gemini. Checked with
the REAL GeminiEngine (policy gate + redaction) on a FakeTransport: the transport sees nothing containing the
canary, and no prompt declares DOCUMENT_TEXT for that document. A synthetic (attested) document is the positive
control: generative answers may use it, but only with DOCUMENT_TEXT declared.
Skips while the documents API is not merged.
"""

from __future__ import annotations

import io
import secrets
import zipfile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from testing.client import api_client_for, requires_tenant_route, route_exists
from testing.flows import items, job_outcome
from testing.sse import events_by_name, parse_sse, read_stream

pytestmark = [pytest.mark.django_db, requires_tenant_route("documents", "AI/ML")]
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
REJECT_STATUSES = (400, 413, 415, 422)

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/></Relationships>'
)


def _docx(document_xml: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document_xml)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _paragraphs(*texts: str) -> str:
    body = "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in texts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )


def _zip_bomb_docx() -> bytes:
    """Valid DOCX structure whose document part inflates from ~20 KB to 24 MiB (ratio ~1000)."""
    return _docx(
        _paragraphs("Synthetic contract"), extra={"word/media/filler.xml": b"\0" * (24 * 1024 * 1024)}
    )


def _xxe_docx(target_path: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<!DOCTYPE w:document [<!ENTITY xxe SYSTEM "file:///{target_path}">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&xxe;</w:t></w:r></w:p></w:body></w:document>"
    )
    return _docx(xml)


def _many_page_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def docs(two_orgs, memory_storage, fake_redis, fake_embedder, django_capture_on_commit_callbacks):
    a = two_orgs.a
    base = f"/api/v1/orgs/{a.slug}/documents"
    if not route_exists(base):
        pytest.skip("documents API not implemented yet (AI/ML, Document Intelligence)")
    client = api_client_for(a.owner)

    def upload(name: str, data: bytes, content_type: str = DOCX, **fields):
        with django_capture_on_commit_callbacks(execute=True):
            return client.post(
                base, {"file": SimpleUploadedFile(name, data, content_type), **fields}, format="multipart"
            )

    def outcome(resp) -> str:
        """'rejected' (4xx or async rejection), 'ready' or another final status."""
        assert resp.status_code < 500, resp.content[:300]
        if resp.status_code in REJECT_STATUSES:
            assert resp.json().get("code"), resp.content[:300]
            return "rejected"
        assert resp.status_code in (201, 202), resp.content[:300]
        body = resp.json()
        doc = body.get("document", body)
        if body.get("job_id"):
            job = job_outcome(client, a.slug, body["job_id"])
            if job["status"] == "failed":
                return "rejected"
        detail = client.get(f"{base}/{doc['id']}").json()
        return "rejected" if detail.get("status") in ("rejected", "failed") else detail.get("status", "?")

    upload.outcome, upload.client, upload.base, upload.world, upload.storage = (
        outcome,
        client,
        base,
        a,
        memory_storage,
    )
    return upload


def test_valid_docx_is_accepted(docs):
    resp = docs("contract.docx", _docx(_paragraphs("Synthetic supplier agreement between Acme and Beta.")))
    assert docs.outcome(resp) == "ready"


@pytest.mark.parametrize(
    ("name", "builder", "content_type"),
    [
        ("bomb.docx", _zip_bomb_docx, DOCX),
        ("image.pdf", lambda: b"\x89PNG\r\n\x1a\n" + b"\0" * 512, "application/pdf"),
        ("program.docx", lambda: b"MZ\x90\x00" + b"\0" * 512, DOCX),
        ("notes.pdf", lambda: b"just some text, not a pdf\n" * 20, "application/pdf"),
        ("pages.pdf", lambda: _many_page_pdf(2001), "application/pdf"),
    ],
    ids=["zip-bomb-docx", "png-as-pdf", "exe-as-docx", "text-as-pdf", "2001-page-pdf"],
)
def test_hostile_documents_are_refused(docs, name, builder, content_type):
    assert docs.outcome(docs(name, builder(), content_type)) == "rejected"
    if docs.storage.keys_for(docs.world.id):
        listed = docs.client.get(docs.base)
        assert all(d.get("status") in ("rejected", "failed") for d in items(listed)), items(listed)


def test_oversized_document_is_refused(docs, settings):
    settings.MAX_UPLOAD_BYTES = 64 * 1024
    settings.DOCUMENT_MAX_BYTES = 64 * 1024
    data = b"%PDF-1.4\n" + b"%" + b"x" * (200 * 1024) + b"\n%%EOF\n"
    resp = docs("big.pdf", data, "application/pdf")
    assert resp.status_code in (400, 413), resp.status_code
    assert docs.storage.keys_for(docs.world.id) == []


def test_xxe_entities_are_never_expanded(docs, tmp_path, caplog):
    secret = f"QA-XXE-CANARY-{secrets.token_hex(6)}"
    target = tmp_path / "secret.txt"
    target.write_text(secret, encoding="utf-8")
    resp = docs("xxe.docx", _xxe_docx(target.as_posix().lstrip("/")))
    status = docs.outcome(resp)
    seen = [resp.content.decode(errors="replace"), caplog.text]
    if status != "rejected":
        body = resp.json()
        doc_id = body.get("document", body)["id"]
        for suffix in ("", "/pages/1", "/entities", "/tables"):
            seen.append(docs.client.get(f"{docs.base}/{doc_id}{suffix}").content.decode(errors="replace"))
    assert not any(secret in s for s in seen), "an external XML entity was resolved (XXE)"


# ------------------------------------------------------------------------------------------------ AI policy
def _ask(docs, question: str):
    """As built (W3 ai_ml handoff), POST /documents/ask answers with one JSON body; an SSE build is also read.

    Returns the SSE events by name, or {"answer": [body]} for the JSON shape. A policy refusal surfaces as an
    `error` event / problem body with code `ai_policy_blocked`."""
    with_ask = f"{docs.base}/ask"
    resp = docs.client.post(with_ask, {"question": question}, format="json")
    if getattr(resp, "streaming", False):
        assert resp.status_code == 200, resp.status_code
        return events_by_name(parse_sse(read_stream(resp)))
    body = resp.json()
    if resp.status_code != 200:
        return {"error": [body]}
    return {"answer": [body]}


def _sent_text(parts) -> list[str]:
    transport = parts["transport"]
    return [f"{r.system}\n{r.prompt}" for r in (*transport.calls, *transport.stream_calls)]


@pytest.mark.ac("P2-AC3")
def test_internal_document_text_never_reaches_gemini_under_synthetic_only(docs, gated_gemini_engine):
    from apps.ai.testing import FakeTransport

    _engine, parts = gated_gemini_engine
    parts["transport"].default = FakeTransport.text("Synthetic answer.")
    canary = f"QA-DOC-CANARY-{secrets.token_hex(6)}"
    resp = docs("handbook.docx", _docx(_paragraphs(f"The retention period is {canary} days.", "Synthetic.")))
    assert docs.outcome(resp) == "ready"  # classification defaults to internal
    if not route_exists(f"{docs.base}/ask"):
        pytest.skip("POST /documents/ask not implemented yet")
    events = _ask(docs, "What is the retention period?")
    assert "error" not in events or events["error"][0].get("code") in ("ai_policy_blocked",), events.get(
        "error"
    )
    assert not any(canary in t for t in _sent_text(parts)), "internal document text was sent to Gemini"
    for record in parts["recorder"].records:
        if "DOCUMENT_TEXT" in record.payload_classes:
            assert record.status == "blocked_by_policy", (record.prompt_key, record.status)


@pytest.mark.ac("P2-AC3")
def test_synthetic_document_text_is_sent_only_with_a_declared_payload(docs, gated_gemini_engine):
    from apps.ai.testing import FakeTransport

    _engine, parts = gated_gemini_engine
    parts["transport"].default = FakeTransport.text("Synthetic answer.")
    marker = f"QA-SYNTH-MARKER-{secrets.token_hex(6)}"
    resp = docs(
        "synthetic.docx",
        _docx(_paragraphs(f"Synthetic policy text {marker}.")),
        classification="synthetic",
        attest="true",
    )
    if docs.outcome(resp) != "ready":
        pytest.skip("documents cannot be uploaded as attested synthetic in this build")
    if not route_exists(f"{docs.base}/ask"):
        pytest.skip("POST /documents/ask not implemented yet")
    _ask(docs, "What does the synthetic policy say?")
    if any(marker in text for text in _sent_text(parts)):
        body = resp.json()
        doc_ref = f"document:{body.get('document', body)['id']}"
        declared = [
            r
            for r in parts["recorder"].records
            if r.status == "ok" and "DOCUMENT_TEXT" in r.payload_classes and doc_ref in r.payload_assets
        ]
        assert declared, "document text was sent without declaring DOCUMENT_TEXT for that document"
