"""Keyword-based document type classification (local): invoice, contract, resume, report, policy, other.

Each type has weighted cue phrases (English plus common Hindi/Gujarati terms). Scores are weighted hit counts
normalised by a soft length factor; the best type wins when it clears an absolute floor and beats the
runner-up by a margin, otherwise `other`."""

from __future__ import annotations

import math
import re

CUES: dict[str, dict[str, float]] = {
    "invoice": {
        "tax invoice": 4, "invoice": 3, "invoice no": 3, "invoice number": 3, "bill to": 3, "ship to": 2,
        "amount due": 3, "total due": 3, "subtotal": 2, "gstin": 2, "hsn": 2, "sac": 1, "cgst": 2, "sgst": 2,
        "igst": 2, "due date": 2, "payment terms": 1, "qty": 1, "unit price": 2, "चालान": 3, "बिल": 2,
        "ઇન્વૉઇસ": 3, "બિલ": 2,
    },
    "contract": {
        "agreement": 3, "this agreement": 4, "hereinafter": 4, "whereas": 3, "parties": 2, "party": 1,
        "terms and conditions": 2, "termination": 2, "governing law": 3, "indemnif": 3, "confidentiality": 2,
        "in witness whereof": 4, "jurisdiction": 2, "effective date": 1, "obligations": 1, "अनुबंध": 4,
        "समझौता": 4, "કરાર": 4,
    },
    "resume": {
        "curriculum vitae": 5, "resume": 4, "work experience": 3, "professional experience": 3, "education": 2,
        "skills": 2, "objective": 1, "certifications": 2, "linkedin": 2, "projects": 1, "references": 1,
        "बायोडाटा": 4, "रिज्यूमे": 4, "બાયોડેટા": 4,
    },
    "report": {
        "report": 3, "executive summary": 4, "findings": 2, "analysis": 2, "conclusion": 2, "recommendations": 2,
        "methodology": 2, "quarter": 1, "q1": 1, "q2": 1, "q3": 1, "q4": 1, "year over year": 2, "kpi": 1,
        "रिपोर्ट": 3, "प्रतिवेदन": 3, "અહેવાલ": 3,
    },
    "policy": {
        "policy": 3, "scope": 1, "applicability": 3, "compliance": 2, "employees shall": 3, "shall": 1,
        "procedure": 2, "violation": 2, "disciplinary": 2, "purpose of this policy": 4, "nonconformance": 1,
        "नीति": 4, "નીતિ": 4,
    },
}
MIN_SCORE = 4.0
MARGIN = 1.15


def _count(text: str, cue: str) -> int:
    if cue.isascii():
        return len(re.findall(rf"(?<![a-z]){re.escape(cue)}", text))
    return text.count(cue)


def score_types(text: str) -> dict[str, float]:
    lowered = (text or "").lower()
    words = max(1, len(lowered.split()))
    norm = 1.0 + math.log10(max(1.0, words / 200))
    scores = {}
    for kind, cues in CUES.items():
        raw = sum(weight * min(_count(lowered, cue), 5) for cue, weight in cues.items())
        scores[kind] = round(raw / norm, 3)
    return scores


def classify_document(text: str, *, title: str = "") -> tuple[str, dict[str, float]]:
    """Returns (doc_type, scores). The title counts double (it is often the strongest cue)."""
    scores = score_types(f"{title}\n{title}\n{text[:200_000]}")
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score >= MIN_SCORE and best_score >= runner * MARGIN:
        return best, scores
    return "other", scores
