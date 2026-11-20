"""Pure text helpers: decoding, script-based language detection (Latin / Devanagari / Gujarati), token
estimation, sentence splitting, heading heuristics and query terms. No I/O, no Django models."""

from __future__ import annotations

import math
import re
import unicodedata

LATIN_RE = re.compile(r"[A-Za-zÀ-ɏ]")
DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")
GUJARATI_RE = re.compile(r"[઀-૿]")
SCRIPT_LANG = (("en", LATIN_RE), ("hi", DEVANAGARI_RE), ("gu", GUJARATI_RE))
PRIMARY_SHARE = 0.6
MIN_LETTERS = 3

# Words: Latin/digits plus combining marks of Indic scripts (\w covers Indic letters; marks are \p{M}).
WORD_RE = re.compile(r"[\wऀ-ॿ઀-૿]+", re.UNICODE)
TOKEN_RE = re.compile(r"[\wऀ-ॿ઀-૿]+|[^\w\s]", re.UNICODE)
# Sentence ends: . ! ? and the Devanagari danda (।) / double danda (॥); keep the delimiter.
SENTENCE_RE = re.compile(r"[^.!?।॥\n]+(?:[.!?।॥]+|$)", re.UNICODE)
NUMBERED_HEADING_RE = re.compile(r"^(?:\d+(?:\.\d+){0,3}\.?|[IVXLC]+\.|[A-Z]\.)\s+\S")
MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")

STOPWORDS = frozenset("""
a an and are as at be been but by can could did do does for from had has have he her his how i if in into is it
its may me my no not of on or our shall she should so than that the their them then there these they this those
to under up us was we were what when where which who whom why will with would you your about above after again
against all also any because before being below between both during each few further here just more most other
over own same some such through too until very
का के की है और में से को पर यह था थे एक ने भी तो ही
છે અને ના ની નું માં થી પર એક આ તે
""".split())


def decode_text(data: bytes) -> str:
    """Decode uploaded text: BOM-aware UTF-8/UTF-16, then UTF-8, then cp1252 (never fails)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def normalize_ws(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace(" ", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def script_shares(text: str) -> dict[str, float]:
    counts = {lang: len(rx.findall(text or "")) for lang, rx in SCRIPT_LANG}
    total = sum(counts.values())
    if total < MIN_LETTERS:
        return {}
    return {lang: round(n / total, 4) for lang, n in counts.items() if n}


def detect_language(text: str) -> str:
    """'en' (Latin script), 'hi' (Devanagari), 'gu' (Gujarati), 'mixed' or 'und' (too little text)."""
    shares = script_shares(text)
    if not shares:
        return "und"
    lang, share = max(shares.items(), key=lambda kv: kv[1])
    return lang if share >= PRIMARY_SHARE else "mixed"


def estimate_tokens(text: str) -> int:
    """Approximate e5/XLM-R sentencepiece tokens: short ASCII words ~1 token, longer words and Indic words split
    into pieces (~4 chars per piece), punctuation 1 token. Conservative for chunk sizing, not billing."""
    n = 0
    for tok in TOKEN_RE.findall(text or ""):
        if tok.isascii() and len(tok) <= 6:
            n += 1
        else:
            n += max(1, math.ceil(len(tok) / 4))
    return n


def split_sentences(text: str) -> list[str]:
    out = []
    for m in SENTENCE_RE.finditer(text or ""):
        s = m.group(0).strip()
        if s:
            out.append(s)
    return out


def looks_like_heading(line: str) -> int:
    """Heading level (1-3) for a plain-text line, 0 when it is body text."""
    s = (line or "").strip()
    if not s or len(s) > 90:
        return 0
    m = MD_HEADING_RE.match(s)
    if m:
        return min(3, len(m.group(1)))
    if s.endswith((".", ",", ";", ":", "?", "!", "।")) and not NUMBERED_HEADING_RE.match(s):
        return 0
    words = WORD_RE.findall(s)
    if not words or len(words) > 12:
        return 0
    if NUMBERED_HEADING_RE.match(s):
        depth = s.split()[0].rstrip(".").count(".")
        return min(3, depth + 1)
    letters = [c for c in s if c.isalpha() and c.isascii()]
    if len(letters) >= 3 and all(c.isupper() for c in letters):
        return 1
    return 0


def heading_text(line: str) -> str:
    s = line.strip()
    m = MD_HEADING_RE.match(s)
    return m.group(2).strip() if m else s


def query_terms(text: str, *, max_terms: int = 12) -> list[str]:
    seen: dict[str, None] = {}
    for w in WORD_RE.findall((text or "").lower()):
        if len(w) < 2 or w in STOPWORDS:
            continue
        seen.setdefault(w, None)
        if len(seen) >= max_terms:
            break
    return list(seen)


def term_spans(text: str, terms: list[str], *, limit: int = 50) -> list[list[int]]:
    """[start, end] offsets of whole-word, case-insensitive occurrences of `terms` in `text` (for highlights)."""
    if not terms:
        return []
    rx = re.compile(r"(?<![\w])(" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
                    + r")(?![\w])", re.IGNORECASE | re.UNICODE)
    return [[m.start(), m.end()] for m in rx.finditer(text or "")][:limit]


_TERMINATORS = (".", "!", "?", ":", ";", "\u0964", "\u0965")


def prose_sentences(text: str) -> list[str]:
    """Sentences of running text: wrapped lines of a paragraph are re-joined, while headings and short
    unterminated lines (titles, table rows, labels) stay on their own so they never glue onto a sentence."""
    paragraphs: list[str] = []
    current: list[str] = []

    def flush():
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    lines = [raw.strip() for raw in (text or "").split("\n")]
    for i, line in enumerate(lines):
        if not line:
            flush()
            continue
        nxt = next((n for n in lines[i + 1:] if n), "")
        continues = bool(nxt) and nxt[0].isalpha() and nxt[0].islower()  # wrapped mid-sentence
        short_label = len(line.split()) <= 8 and not line.endswith(_TERMINATORS) and not continues
        if looks_like_heading(line) or short_label:
            flush()
            paragraphs.append(line)
            continue
        current.append(line)
    flush()
    return [s for p in paragraphs for s in split_sentences(p)]
