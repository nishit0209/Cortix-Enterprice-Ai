"""Isolation-matrix factories for apps.documents routes (see backend/testing/isolation.py)."""

from __future__ import annotations


def _document(world) -> dict[str, str]:
    doc = world.resources.get("document")
    if doc is None:
        from apps.documents.tests.factories import make_document

        with world.tenant():
            doc = make_document(title="Isolation probe document")
        world.resources["document"] = doc
    return {"pk": str(doc.id)}


ISOLATION_FACTORIES = {
    name: _document
    for name in (
        "document-detail", "document-pages", "document-chunks", "document-entities", "document-tables",
        "document-summary", "document-classification", "document-reprocess",
    )
}
