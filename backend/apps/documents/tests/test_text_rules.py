"""Language detection, token estimate, headings, chunking, entities and keyword classification (pure)."""

from __future__ import annotations

import pytest

from apps.documents.chunking import chunk_pages
from apps.documents.classify import classify_document
from apps.documents.entities import extract_entities, verhoeff_valid
from apps.documents.extraction import Block, PageContent, TableData
from apps.documents.textutil import (
    detect_language,
    estimate_tokens,
    looks_like_heading,
    query_terms,
    script_shares,
    split_sentences,
    term_spans,
)

from . import synth


# ---------------------------------------------------------------------------------------------- language
@pytest.mark.parametrize(("text", "lang"), [
    ("The quarterly revenue grew in the North region.", "en"),
    (synth.HINDI, "hi"),
    (synth.GUJARATI, "gu"),
    ("12345 67890 !!!", "und"),
    ("Revenue report " + synth.HINDI[:20] + " " + synth.GUJARATI[:20], "mixed"),
])
def test_detect_language(text, lang):
    assert detect_language(text) == lang


def test_script_shares_sum_to_one():
    shares = script_shares("abc " + synth.HINDI)
    assert set(shares) == {"en", "hi"} and abs(sum(shares.values()) - 1) < 0.01


def test_token_estimate_is_monotonic_and_indic_aware():
    assert estimate_tokens("") == 0
    assert estimate_tokens("one two three") == 3
    assert estimate_tokens("internationalization") >= 4
    assert estimate_tokens(synth.HINDI) > len(synth.HINDI.split())


def test_sentences_split_on_danda_and_period():
    assert len(split_sentences(synth.HINDI)) == 3
    assert split_sentences("First one. Second one? Third!") == ["First one.", "Second one?", "Third!"]


@pytest.mark.parametrize(("line", "level"), [
    ("# Title", 1), ("### Deep", 3), ("1. Introduction", 1), ("2.3 Scope of Work", 2), ("EXECUTIVE SUMMARY", 1),
    ("This is a normal sentence.", 0), ("", 0), ("x" * 120, 0),
])
def test_heading_heuristic(line, level):
    assert looks_like_heading(line) == level


def test_query_terms_and_spans():
    terms = query_terms("What is the total revenue for the North region?")
    assert terms == ["total", "revenue", "north", "region"]
    text = "North revenue rose; northern stores and REVENUE totals."
    spans = term_spans(text, ["north", "revenue"])
    assert [text[s:e] for s, e in spans] == ["North", "revenue", "REVENUE"]


# ---------------------------------------------------------------------------------------------- chunking
def _page(number, blocks):
    return PageContent(number=number, text="\n".join(b.text for b in blocks), blocks=blocks)


def _sentences(n, prefix="Sentence"):
    return " ".join(f"{prefix} number {i} talks about synthetic revenue figures." for i in range(n))


def test_chunks_respect_target_and_overlap():
    pages = [_page(1, [Block("heading", "Overview", 1), Block("paragraph", _sentences(120))])]
    chunks = chunk_pages(pages, target=200, overlap=40)
    assert len(chunks) > 3
    assert all(c.token_count <= 200 + 45 for c in chunks)
    for a, b in zip(chunks, chunks[1:], strict=False):
        tail = a.text.split()[-4:]
        assert " ".join(tail) in b.text, "consecutive chunks in one section overlap"
    assert all(c.section == "Overview" for c in chunks)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_headings_start_new_chunks_without_overlap_and_build_paths():
    pages = [
        _page(1, [Block("heading", "Agreement", 1), Block("paragraph", "Intro paragraph about the parties."),
                  Block("heading", "Termination", 2), Block("paragraph", "Notice period is thirty days.")]),
        _page(2, [Block("heading", "Payment", 2), Block("paragraph", "Fees are due within 15 days.")]),
    ]
    chunks = chunk_pages(pages)
    assert [c.section for c in chunks] == ["Agreement", "Agreement > Termination", "Agreement > Payment"]
    assert chunks[1].text.startswith("Termination") and "Intro paragraph" not in chunks[1].text
    assert (chunks[2].page, chunks[2].page_to) == (2, 2)


def test_chunk_spanning_pages_keeps_page_range():
    pages = [_page(1, [Block("paragraph", "End of page one text.")]),
             _page(2, [Block("paragraph", "Start of page two text.")])]
    [chunk] = chunk_pages(pages)
    assert (chunk.page, chunk.page_to) == (1, 2)


def test_tables_are_kept_intact_or_split_with_header():
    small = TableData(header=["a", "b"], rows=[["1", "2"], ["3", "4"]])
    pages = [_page(1, [Block("paragraph", _sentences(3)), Block("table", small.as_text())])]
    chunks = chunk_pages(pages, target=500)
    assert any(small.as_text() in c.text for c in chunks)
    big = TableData(header=["region", "units"], rows=[[f"R{i}", str(i)] for i in range(400)])
    chunks = chunk_pages([_page(1, [Block("table", big.as_text())])], target=120, overlap=20)
    assert len(chunks) > 3
    assert all(c.text.split("\n")[0] == "region | units" for c in chunks)


def test_empty_pages_produce_no_chunks():
    assert chunk_pages([PageContent(number=1, method="ocr_unavailable")]) == []


# ---------------------------------------------------------------------------------------------- entities
def _by_kind(text):
    out = {}
    for e in extract_entities(text):
        out.setdefault(e.kind, []).append(e)
    return out


def test_invoice_entities():
    ents = _by_kind(synth.INVOICE_TEXT)
    assert [e.normalized for e in ents["email"]] == ["billing@acme-synthetic.example"]
    assert ents["email"][0].is_pii
    assert ents["gstin"][0].value == "27ABCDE1234F1Z5" and not ents["gstin"][0].is_pii
    assert "pan" not in ents, "the PAN inside a GSTIN is not reported twice"
    assert {e.normalized for e in ents["date"]} == {"2026-03-15", "2026-04-14"}
    amounts = {e.normalized for e in ents["amount"]}
    assert {"INR 120000.00", "INR 141600.00", "USD 1700.00"} <= amounts
    assert ents["phone"][0].normalized == "+919876543210" and ents["phone"][0].is_pii
    assert any(e.value == "Acme Synthetic Traders Pvt Ltd" for e in ents["org"])


def test_pan_and_aadhaar_like_ids():
    # 2345 6789 0124 is not checksum-valid; build a valid one with the Verhoeff check digit.
    from apps.documents.entities import _D, _P

    def check_digit(num: str) -> str:
        for d in range(10):
            if verhoeff_valid(num + str(d)):
                return str(d)
        raise AssertionError

    assert _D and _P
    base = "23456789012"
    valid = base + check_digit(base)
    text = f"PAN ABCPE1234F and id {valid[:4]} {valid[4:8]} {valid[8:]} and bad id 2345 6789 0120"
    ents = _by_kind(text)
    assert ents["pan"][0].value == "ABCPE1234F" and ents["pan"][0].is_pii
    assert [e.normalized for e in ents.get("aadhaar", [])] == [valid]


def test_amount_scales_and_dates():
    ents = _by_kind("Budget of ₹ 5 lakh and Rs 2.5 crore, paid on 3rd March 2026 and March 4, 2026; $1.2 million")
    amounts = {e.normalized for e in ents["amount"]}
    assert {"INR 500000.00", "INR 25000000.00", "USD 1200000.00"} <= amounts
    assert {e.normalized for e in ents["date"]} == {"2026-03-03", "2026-03-04"}


def test_invalid_dates_are_ignored():
    assert "date" not in _by_kind("Reference 31/02/2026 and 2026-13-01")


# ------------------------------------------------------------------------------------------ classification
@pytest.mark.parametrize(("text", "expected"), [
    (synth.INVOICE_TEXT, "invoice"),
    ("This Agreement is entered into by the parties, hereinafter the Client. WHEREAS the parties wish to "
     "agree. Termination and governing law clauses apply. IN WITNESS WHEREOF the parties sign.", "contract"),
    ("Curriculum Vitae. Work Experience: data analyst. Education: B.Tech. Skills: SQL, Python. "
     "Certifications: none. LinkedIn profile available.", "resume"),
    ("Quarterly Report. Executive Summary. Findings and analysis of Q1 sales. Recommendations and "
     "conclusion. Methodology described below.", "report"),
    ("Information Security Policy. Purpose of this policy. Applicability: all employees shall comply. "
     "Violation leads to disciplinary procedure.", "policy"),
    ("A short note about lunch plans for Friday.", "other"),
])
def test_classify_document(text, expected):
    doc_type, scores = classify_document(text)
    assert doc_type == expected
    assert set(scores) == {"invoice", "contract", "resume", "report", "policy"}


def test_classify_hindi_contract_keywords():
    doc_type, _ = classify_document("यह अनुबंध दोनों पक्षों के बीच समझौता है। अनुबंध की शर्तें लागू हैं।")
    assert doc_type == "contract"


def test_prose_sentences_rejoin_wrapped_lines_and_isolate_headings():
    from apps.documents.textutil import prose_sentences

    text = "Findings\nThe North region sold 120\nunits in January. South was flat.\nRegion | Units\nNorth | 120"
    assert prose_sentences(text) == ["Findings", "The North region sold 120 units in January.", "South was flat.",
                                     "Region | Units", "North | 120"]
