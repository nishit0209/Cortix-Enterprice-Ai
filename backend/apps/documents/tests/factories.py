"""Synthetic Document rows for DB tests (inside a tenant context)."""

from __future__ import annotations

import hashlib

from django.contrib.postgres.search import SearchVector

from apps.ai.embeddings import FakeEmbedder
from apps.core import storage
from apps.documents.models import Document, DocumentChunk, DocumentPage


def make_document(*, title: str = "Synthetic document", text: str = "Synthetic revenue grew in the North region.",
                  classification: str = "internal", status: str = "ready", embed: bool = True) -> Document:
    doc = Document(title=title, filename="synthetic.txt", mime="text/plain", file_kind="txt",
                   size_bytes=len(text.encode()), sha256=hashlib.sha256(text.encode()).hexdigest(),
                   classification=classification, status=status, page_count=1, chunk_count=1, language="en")
    doc.storage_key = storage.org_key("documents", str(doc.id), "synthetic.txt")
    doc.save()
    DocumentPage.objects.create(document=doc, number=1, text=text, char_count=len(text), language="en")
    vector = [float(x) for x in FakeEmbedder().embed_passages([text])[0]] if embed else None
    DocumentChunk.objects.create(document=doc, ordinal=0, text=text, page=1, page_to=1, token_count=len(text.split()),
                                 embedding=vector)
    DocumentChunk.objects.filter(document=doc).update(search_vector=SearchVector("text", config="simple"))
    return doc
