"""Isolation-matrix factories for apps.documents routes (see backend/testing/isolation.py)."""

from __future__ import annotations


def _doc(world):
    doc = world.resources.get("document")
    if doc is None:
        from apps.documents.tests.factories import make_document

        with world.tenant():
            doc = make_document(title="Isolation probe document")
        world.resources["document"] = doc
    return doc


def _document(world) -> dict[str, str]:
    return {"pk": str(_doc(world).id)}


def _collection(world) -> dict[str, str]:
    coll = world.resources.get("document_collection")
    if coll is None:
        from apps.documents.models import DocumentCollection

        with world.tenant():
            coll = DocumentCollection.objects.create(name="Isolation probe collection")
        world.resources["document_collection"] = coll
    return {"pk": str(coll.id)}


def _extraction(world) -> dict[str, str]:
    ext = world.resources.get("document_extraction")
    if ext is None:
        from apps.documents.models import ExtractionResult

        doc = _doc(world)
        with world.tenant():
            ext = ExtractionResult.objects.create(document=doc, schema_key="invoice", result={"invoice_number": None},
                                                  field_citations={"invoice_number": None})
        world.resources["document_extraction"] = ext
    return {"pk": str(ext.id)}


def _table(world) -> dict[str, str]:
    table = world.resources.get("document_table")
    if table is None:
        from apps.documents.models import DocumentTable

        doc = _doc(world)
        with world.tenant():
            table = DocumentTable.objects.create(document=doc, page=1, index=0, header=["Region", "Units"],
                                                 rows=[["North", "1"]], n_rows=1, n_cols=2)
        world.resources["document_table"] = table
    return {"pk": str(table.id)}


ISOLATION_FACTORIES = {
    **{
        name: _document
        for name in (
            "document-detail", "document-pages", "document-chunks", "document-entities", "document-tables",
            "document-summary", "document-classification", "document-reprocess", "document-extract",
            "document-extractions",
        )
    },
    "document-collection-detail": _collection,
    "document-extraction-detail": _extraction,
    "document-extraction-review": _extraction,
    "document-table-detail": _table,
    "document-table-promote": _table,
}
