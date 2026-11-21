"""Document intelligence, API_SPECIFICATION Phase 8 rows added in W5 (re-exported by apps.documents.services).

    collections        create_collection / update_collection / delete_collection / collection_payload
    extract(doc, schema_key, mode)   local rules (default) or Gemini (policy-gated, quotes verified) -> ExtractionResult
    review_extraction(result, decision, corrections)                 needs_review -> approved | rejected
    compare(a, b, summary=False)     local section alignment + word diff (+ optional policy-gated Gemini summary)
    promote_table(table, ...)        DocumentTable -> dataset through apps.datasets.services.publish_frame
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import OrderedDict
from datetime import date

import polars as pl
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError

from apps.core import audit

from . import structured
from .exceptions import AttestationRequired, DocumentNotReady
from .models import (
    ATTESTED_CLASSIFICATIONS,
    CLASSIFICATIONS,
    Document,
    DocumentChunk,
    DocumentCollection,
    DocumentEntity,
    DocumentPage,
    DocumentTable,
    ExtractionResult,
)

log = logging.getLogger("nexora.documents")

MAX_DIFF_LINES_PER_SECTION = 80
MAX_DIFF_SECTIONS = 60
SAME_THRESHOLD = 0.985
MATCH_CUTOFF = 0.72


# ------------------------------------------------------------------------------------------- collections
def get_collection(collection_id) -> DocumentCollection:
    try:
        return DocumentCollection.objects.get(id=collection_id)
    except (DocumentCollection.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        raise NotFound() from exc


def create_collection(*, name: str, description: str = "", classification: str = "internal", user_id=None,
                      request=None) -> DocumentCollection:
    name = (name or "").strip()[:200]
    if not name:
        raise ValidationError({"name": ["A name is required."]})
    if classification not in dict(CLASSIFICATIONS):
        raise ValidationError({"classification": [f"Must be one of {', '.join(dict(CLASSIFICATIONS))}."]})
    try:
        with transaction.atomic():
            coll = DocumentCollection.objects.create(name=name, description=description or "",
                                                     classification=classification, created_by=user_id)
    except IntegrityError as exc:
        raise ValidationError({"name": ["A collection with this name already exists."]}) from exc
    audit.record("document.collection.create", target=coll, request=request, metadata={"name": name})
    return coll


def update_collection(coll: DocumentCollection, data: dict, *, request=None) -> DocumentCollection:
    before, after = {}, {}
    for field in ("name", "description", "classification"):
        if field in data and data[field] is not None and getattr(coll, field) != data[field]:
            if field == "name" and not str(data[field]).strip():
                raise ValidationError({"name": ["A name is required."]})
            before[field], after[field] = getattr(coll, field), data[field]
            setattr(coll, field, data[field])
    if after:
        try:
            with transaction.atomic():
                coll.save()
        except IntegrityError as exc:
            raise ValidationError({"name": ["A collection with this name already exists."]}) from exc
        audit.record("document.collection.update", target=coll, request=request,
                     changes={"before": before, "after": after})
    return coll


def delete_collection(coll: DocumentCollection, *, request=None) -> None:
    """The documents stay (their collection becomes empty)."""
    audit.record("document.collection.delete", target=coll, request=request,
                 metadata={"name": coll.name, "documents": coll.documents.count()})
    coll.delete()


def set_document_collection(doc: Document, collection_id, *, request=None) -> Document:
    coll = get_collection(collection_id) if collection_id else None
    if (coll.id if coll else None) != doc.collection_id:
        before = str(doc.collection_id) if doc.collection_id else None
        doc.collection = coll
        doc.save(update_fields=["collection", "updated_at"])
        audit.record("document.collection.assign", target=doc, request=request,
                     changes={"before": {"collection_id": before},
                              "after": {"collection_id": str(coll.id) if coll else None}})
    return doc


# ------------------------------------------------------------------------------------------- extraction
def _pages(doc: Document) -> list[tuple[int, str]]:
    return list(DocumentPage.objects.filter(document=doc).order_by("number").values_list("number", "text"))


def _coerce(spec: structured.FieldSpec, raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    if spec.type == "date":
        return structured._date_value(raw)
    if spec.type == "amount":
        return structured.parse_amount(raw)[0]
    if spec.type == "integer":
        m = re.search(r"\d+", raw)
        return int(m.group(0)) if m else None
    if spec.type == "list":
        items = [i.strip() for i in re.split(r"[,;|\n]", raw) if i.strip()]
        return items or None
    if spec.name == "email":
        return raw.lower()
    return raw[:300]


def _gemini_fields(doc: Document, schema_key: str, *, user_id) -> tuple[structured.Extraction, list]:
    """Gemini extraction (policy gate: DOCUMENT_TEXT on this document). Values without a verified verbatim quote
    are dropped (null), so every non-null field is cited."""
    from apps.ai.services import gemini, get_prompt, request_trace

    from .ranking import Passage, build_sources, quote_matches
    from .services import _payload_for

    chunks = list(DocumentChunk.objects.filter(document=doc).order_by("ordinal")[:60])
    passages = [Passage(chunk_id=str(c.id), document_id=str(doc.id), document_title=doc.title, page=c.page,
                        page_to=c.page_to, section=c.section, text=c.text) for c in chunks]
    sources = build_sources(passages)
    specs = structured.SCHEMAS[schema_key]["fields"]
    prompt = get_prompt("docs.extract")
    with request_trace() as ids:
        data = gemini().generate_structured(
            feature="documents.extract", prompt_key=prompt.key, system=prompt.system,
            prompt=prompt.render(schema_label=structured.SCHEMAS[schema_key]["label"],
                                 fields=[{"name": f.name, "type": f.type, "description": f.description} for f in specs],
                                 sources=sources),
            schema=prompt.response_schema, payload=_payload_for(sources), tier=prompt.tier, user_id=user_id)
    by_id = {s["source_id"]: s for s in sources}
    page_text = dict(_pages(doc))
    returned = {str(f.get("name")): f for f in (data.get("fields") or []) if isinstance(f, dict)}
    out = structured.Extraction(schema_key=schema_key)
    for spec in specs:
        item = returned.get(spec.name) or {}
        source = by_id.get(str(item.get("source_id") or ""))
        quote = str(item.get("quote") or "").strip()
        value = _coerce(spec, str(item.get("value") or ""))
        if value is None or source is None or not quote or not quote_matches(quote, source["text"]):
            out.result[spec.name], out.citations[spec.name], out.confidence[spec.name] = None, None, None
            continue
        text = page_text.get(source["page"], "")
        start = text.lower().find(quote.lower())
        out.result[spec.name] = value
        out.citations[spec.name] = [{"page": source["page"], "start": start if start >= 0 else None,
                                     "end": start + len(quote) if start >= 0 else None, "quote": quote[:300]}]
        out.confidence[spec.name] = 0.8
    return out, list(ids)


def extract(doc: Document, schema_key: str, *, mode: str = "local", user_id=None, request=None) -> ExtractionResult:
    if schema_key not in structured.SCHEMAS:
        raise ValidationError({"schema_key": [f"Use one of {', '.join(structured.schema_keys())}."]})
    if mode not in ("local", "ai"):
        raise ValidationError({"mode": ["Use 'local' or 'ai'."]})
    if doc.status != "ready":
        raise DocumentNotReady()
    method, reason, ai_ids = "local", "", []
    extraction = None
    if mode == "ai":
        from .services import _AI_ERRORS, _ai_reason

        try:
            extraction, ai_ids = _gemini_fields(doc, schema_key, user_id=user_id)
            method = "gemini"
        except _AI_ERRORS as exc:
            reason = _ai_reason(exc)
    if extraction is None:
        extraction = structured.extract_fields(schema_key, _pages(doc))
    result = ExtractionResult.objects.create(
        document=doc, schema_key=schema_key, method=method, result=extraction.result,
        field_citations=extraction.citations, field_confidence=extraction.confidence, status="needs_review",
        reason=reason, created_by=user_id)
    audit.record("document.extract", target=doc, request=request, metadata={
        "extraction_id": str(result.id), "schema_key": schema_key, "method": method, "reason": reason or None,
        "fields_found": extraction.fields_found, "fields_total": len(extraction.result),
        "ai_request_ids": [str(i) for i in ai_ids]})
    return result


def get_extraction(extraction_id) -> ExtractionResult:
    try:
        return ExtractionResult.objects.select_related("document").get(id=extraction_id)
    except (ExtractionResult.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        raise NotFound() from exc


def review_extraction(result: ExtractionResult, *, decision: str, corrections: dict | None = None, note: str = "",
                      user_id=None, request=None) -> ExtractionResult:
    if decision not in ("approve", "reject"):
        raise ValidationError({"decision": ["Use 'approve' or 'reject'."]})
    if result.status not in ("needs_review", "extracted"):
        raise ValidationError({"status": [f"This extraction is already {result.status}."]})
    corrections = corrections or {}
    names = set(structured.field_names(result.schema_key))
    unknown = sorted(set(corrections) - names)
    if unknown:
        raise ValidationError({"corrections": [f"Unknown field(s): {', '.join(unknown)}."]})
    if decision == "approve" and corrections:
        specs = {f.name: f for f in structured.SCHEMAS[result.schema_key]["fields"]}
        values = dict(result.result)
        cites = dict(result.field_citations)
        for name, value in corrections.items():
            values[name] = _coerce(specs[name], ", ".join(value) if isinstance(value, list) else str(value)) \
                if value not in (None, "") else None
            cites[name] = [{"corrected_by": str(user_id) if user_id else None}] if values[name] is not None else None
        result.result, result.field_citations, result.corrections = values, cites, corrections
    result.status = "approved" if decision == "approve" else "rejected"
    result.reviewed_by, result.reviewed_at, result.review_note = user_id, timezone.now(), (note or "")[:2000]
    result.save()
    audit.record(f"document.extraction.{result.status}", target=result.document, request=request, metadata={
        "extraction_id": str(result.id), "schema_key": result.schema_key, "corrected_fields": sorted(corrections)})
    return result


# ------------------------------------------------------------------------------------------- comparison
_WS = re.compile(r"\s+")


def _norm_title(title: str) -> str:
    return _WS.sub(" ", re.sub(r"^[\d.)\s]+", "", (title or "").lower())).strip()


def _sections(doc: Document) -> OrderedDict[str, dict]:
    """Section title -> {title, pages, text}, from the heading-aware chunks (page-based when there are none)."""
    out: OrderedDict[str, dict] = OrderedDict()
    rows = DocumentChunk.objects.filter(document=doc).order_by("ordinal").values_list("section", "page", "text")
    for section, page, text in rows:
        path = (section or "").strip() or f"Page {page}"
        key = _norm_title(path) or f"page {page}"
        item = out.setdefault(key, {"title": path.split(" > ")[-1], "path": path, "pages": [], "text": []})
        if page not in item["pages"]:
            item["pages"].append(page)
        item["text"].append(text)
    if not out:
        for number, text in _pages(doc):
            out[f"page {number}"] = {"title": f"Page {number}", "path": f"Page {number}", "pages": [number],
                                     "text": [text]}
    for item in out.values():
        item["text"] = _WS.sub(" ", " ".join(item["text"])).strip()
    return out


def _word_diff(a: str, b: str) -> list[dict]:
    wa, wb = a.split(" "), b.split(" ")
    ops = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        ops.append({"op": tag, "a": " ".join(wa[i1:i2])[:500], "b": " ".join(wb[j1:j2])[:500]})
        if len(ops) >= MAX_DIFF_LINES_PER_SECTION:
            break
    return ops


def _entity_values(doc: Document) -> dict[str, set[str]]:
    vals: dict[str, set[str]] = {}
    for kind, norm in DocumentEntity.objects.filter(document=doc, kind__in=("amount", "date", "org", "gstin"))\
            .values_list("kind", "normalized"):
        vals.setdefault(kind, set()).add(norm)
    return vals


def compare(a: Document, b: Document, *, summary: bool = False, user_id=None) -> dict:
    for doc in (a, b):
        if doc.status != "ready":
            raise DocumentNotReady(f"Document {doc.id} is not ready.")
    sa, sb = _sections(a), _sections(b)
    unmatched_b = list(sb)
    pairs: list[tuple[str | None, str | None]] = []
    for key in sa:
        match = key if key in sb and key in unmatched_b else None
        if match is None:  # fuzzy on the section's own heading (a shared parent path would inflate the score)
            leaf = {_norm_title(sb[k]["title"]): k for k in unmatched_b}
            close = difflib.get_close_matches(_norm_title(sa[key]["title"]), list(leaf), n=1, cutoff=MATCH_CUTOFF)
            match = leaf[close[0]] if close else None
        if match:
            unmatched_b.remove(match)
        pairs.append((key, match))
    pairs += [(None, key) for key in unmatched_b]
    sections, total_a, total_b, same_chars = [], 0, 0, 0.0
    for ka, kb in pairs[:MAX_DIFF_SECTIONS]:
        ia, ib = sa.get(ka) if ka else None, sb.get(kb) if kb else None
        ta, tb = (ia or {}).get("text", ""), (ib or {}).get("text", "")
        ratio = difflib.SequenceMatcher(None, ta.split(" "), tb.split(" "), autojunk=False).ratio() if ia and ib \
            else 0.0
        status = "removed" if ib is None else "added" if ia is None else "unchanged" if ratio >= SAME_THRESHOLD \
            else "changed"
        total_a += len(ta)
        total_b += len(tb)
        same_chars += ratio * max(len(ta), len(tb))
        sections.append({
            "status": status, "title_a": (ia or {}).get("title"), "title_b": (ib or {}).get("title"),
            "path_a": (ia or {}).get("path"), "path_b": (ib or {}).get("path"),
            "pages_a": (ia or {}).get("pages", []), "pages_b": (ib or {}).get("pages", []),
            "similarity": round(ratio, 4), "changes": _word_diff(ta, tb) if status == "changed" else [],
        })
    ea, eb = _entity_values(a), _entity_values(b)
    entity_changes = {k: {"added": sorted(eb.get(k, set()) - ea.get(k, set()))[:50],
                          "removed": sorted(ea.get(k, set()) - eb.get(k, set()))[:50]}
                      for k in sorted(set(ea) | set(eb))}
    counts = {s: sum(1 for x in sections if x["status"] == s) for s in ("unchanged", "changed", "added", "removed")}
    out = {
        "a": {"id": str(a.id), "title": a.title}, "b": {"id": str(b.id), "title": b.title},
        "similarity": round(same_chars / max(total_a, total_b, 1), 4), "counts": counts, "sections": sections,
        "entity_changes": {k: v for k, v in entity_changes.items() if v["added"] or v["removed"]},
        "truncated": len(pairs) > MAX_DIFF_SECTIONS, "summary": None, "summary_mode": None, "reason": None,
        "ai_request_ids": [],
    }
    if summary:
        out.update(_compare_summary(a, b, sections, user_id=user_id))
    return out


def _compare_summary(a: Document, b: Document, sections: list[dict], *, user_id) -> dict:
    from apps.ai.services import gemini, get_prompt, request_trace
    from nexora.contracts import PayloadClass, PayloadManifest

    from .services import _AI_ERRORS, _ai_reason

    lines = []
    for s in sections:
        if s["status"] == "unchanged":
            continue
        lines.append(f"[{s['status']}] {s['title_a'] or s['title_b']}")
        lines += [f"  - {c['a']}\n  + {c['b']}" for c in s["changes"][:20]]
    diff = "\n".join(lines)[:12000] or "No differences."
    prompt = get_prompt("docs.compare")
    payload = PayloadManifest({PayloadClass.DOCUMENT_TEXT: (f"document:{a.id}", f"document:{b.id}")})
    with request_trace() as ids:
        try:
            data = gemini().generate_structured(
                feature="documents.compare", prompt_key=prompt.key, system=prompt.system,
                prompt=prompt.render(title_a=a.title, title_b=b.title, diff=diff), schema=prompt.response_schema,
                payload=payload, tier=prompt.tier, user_id=user_id)
        except _AI_ERRORS as exc:
            return {"summary": None, "summary_mode": None, "reason": _ai_reason(exc),
                    "ai_request_ids": [str(i) for i in ids]}
    return {"summary": str(data.get("summary") or "").strip(), "key_changes": [str(k) for k in (
        data.get("key_changes") or [])][:8], "summary_mode": "generative", "reason": None,
            "ai_request_ids": [str(i) for i in ids]}


# ------------------------------------------------------------------------------------------- table promotion
def get_table(table_id) -> DocumentTable:
    try:
        return DocumentTable.objects.select_related("document").get(id=table_id)
    except (DocumentTable.DoesNotExist, ValueError, TypeError, ValidationError) as exc:
        raise NotFound() from exc


def table_frame(table: DocumentTable) -> pl.DataFrame:
    """Header -> unique, non-empty column names; rows padded/cut to the header width; all values as text
    (ingestion infers the types)."""
    width = max([len(table.header or [])] + [len(r) for r in (table.rows or [])] or [0])
    if width == 0:
        raise ValidationError({"table": ["The table has no columns."]})
    names: list[str] = []
    for i in range(width):
        raw = str((table.header or [])[i] if i < len(table.header or []) else "").strip()
        base = re.sub(r"\s+", " ", raw)[:60] or f"column_{i + 1}"
        name, n = base, 2
        while name.lower() in {x.lower() for x in names}:
            name, n = f"{base}_{n}", n + 1
        names.append(name)
    rows = [[(str(v) if v is not None else None) for v in (list(r) + [None] * width)[:width]] for r in table.rows or []]
    if not rows:
        raise ValidationError({"table": ["The table has no rows."]})
    return pl.DataFrame(rows, schema={n: pl.Utf8 for n in names}, orient="row")


def promote_table(table: DocumentTable, *, user_id, name: str = "", classification: str | None = None,
                  attest: bool = False, request=None) -> dict:
    from apps.datasets.services import publish_frame

    doc = table.document
    frame = table_frame(table)
    classification = classification or doc.classification
    if classification not in dict(CLASSIFICATIONS):
        raise ValidationError({"classification": [f"Must be one of {', '.join(dict(CLASSIFICATIONS))}."]})
    if classification in ATTESTED_CLASSIFICATIONS and not attest:
        raise AttestationRequired("Publishing as synthetic or public requires attest=true.")
    order = [c for c, _ in CLASSIFICATIONS]
    if order.index(classification) < order.index(doc.classification):
        if not (attest and classification in ATTESTED_CLASSIFICATIONS):
            raise ValidationError({"classification": ["A table cannot be published less restricted than its "
                                                      "document without an attestation."]})
    title = (name or "").strip() or f"{doc.title} - table {table.index + 1}"
    version = publish_frame(
        name=title[:200], frame=frame, user_id=user_id, classification=classification, attest=attest,
        description=f"Table {table.index + 1} (page {table.page}) of document '{doc.title}', promoted on "
                    f"{date.today().isoformat()}." + (" The table was truncated at extraction; only the stored rows "
                                                       "were published." if table.truncated else ""),
        source=("document", doc.id))
    DocumentTable.objects.filter(id=table.id).update(promoted_dataset_id=version.dataset_id, updated_at=timezone.now())
    audit.record("document.table.promote", target=doc, request=request, metadata={
        "table_id": str(table.id), "dataset_id": str(version.dataset_id), "version": version.version,
        "rows": frame.height, "columns": frame.width, "classification": classification})
    return {"table_id": str(table.id), "dataset_id": str(version.dataset_id), "version_id": str(version.id),
            "version": version.version, "status": version.status,
            "job_id": str(version.job_id) if getattr(version, "job_id", None) else None,
            "rows": frame.height, "columns": frame.columns, "truncated": table.truncated}
