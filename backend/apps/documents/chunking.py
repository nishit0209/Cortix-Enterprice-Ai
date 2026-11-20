"""Structure-aware chunking (AI_ARCHITECTURE.md §7.2): ~500 tokens with ~80 tokens of overlap inside a section,
headings start a new chunk (and form the `section` path), tables are kept intact (a table larger than a chunk is
split by rows with its header repeated), and every chunk keeps its page range for citations."""

from __future__ import annotations

from dataclasses import dataclass

from .extraction import Block, PageContent
from .textutil import WORD_RE, estimate_tokens, split_sentences

TARGET_TOKENS = 500
OVERLAP_TOKENS = 80
MAX_SECTION_PATH = 500


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    text: str
    page: int
    page_to: int
    section: str
    token_count: int


@dataclass
class _Unit:
    text: str
    page: int
    tokens: int
    is_table: bool = False


def _split_long_text(text: str, page: int, target: int) -> list[_Unit]:
    """Paragraph -> sentences -> word windows, so no unit exceeds `target` tokens."""
    units: list[_Unit] = []
    for sentence in split_sentences(text) or [text]:
        tokens = estimate_tokens(sentence)
        if tokens <= target:
            units.append(_Unit(sentence, page, tokens))
            continue
        words = sentence.split()
        window: list[str] = []
        for w in words:
            window.append(w)
            if estimate_tokens(" ".join(window)) >= target:
                units.append(_Unit(" ".join(window), page, estimate_tokens(" ".join(window))))
                window = []
        if window:
            units.append(_Unit(" ".join(window), page, estimate_tokens(" ".join(window))))
    return units


def _table_units(text: str, page: int, target: int) -> list[_Unit]:
    tokens = estimate_tokens(text)
    if tokens <= target:
        return [_Unit(text, page, tokens, is_table=True)]
    lines = text.split("\n")
    header, rows = (lines[0], lines[1:]) if len(lines) > 1 else ("", lines)
    units, current = [], [header] if header else []
    for row in rows:
        candidate = "\n".join([*current, row])
        if current and len(current) > (1 if header else 0) and estimate_tokens(candidate) > target:
            units.append(_Unit("\n".join(current), page, estimate_tokens("\n".join(current)), is_table=True))
            current = [header, row] if header else [row]
        else:
            current.append(row)
    if current and (len(current) > 1 or not header):
        units.append(_Unit("\n".join(current), page, estimate_tokens("\n".join(current)), is_table=True))
    return units


def _overlap_tail(units: list[_Unit], budget: int) -> list[_Unit]:
    """Trailing non-table units totalling <= budget tokens (a trailing slice of words if one unit is too big)."""
    tail: list[_Unit] = []
    total = 0
    for unit in reversed(units):
        if unit.is_table:
            break
        if total + unit.tokens <= budget:
            tail.insert(0, unit)
            total += unit.tokens
            continue
        if not tail:
            words = WORD_RE.findall(unit.text)
            words_kept: list[str] = []
            for w in reversed(unit.text.split()):
                words_kept.insert(0, w)
                if estimate_tokens(" ".join(words_kept)) >= budget:
                    break
            if words and words_kept:
                text = " ".join(words_kept)
                tail.insert(0, _Unit(text, unit.page, estimate_tokens(text)))
        break
    return tail


def chunk_pages(pages: list[PageContent], *, target: int = TARGET_TOKENS,
                overlap: int = OVERLAP_TOKENS) -> list[Chunk]:
    chunks: list[Chunk] = []
    path: list[tuple[int, str]] = []  # (level, heading)
    current: list[_Unit] = []
    has_new = False  # current holds more than the carried-over overlap

    def section() -> str:
        return " > ".join(h for _, h in path)[:MAX_SECTION_PATH]

    def emit():
        nonlocal current, has_new
        if not current or not has_new:
            return
        text = "\n".join(u.text for u in current).strip()
        if text:
            chunks.append(Chunk(len(chunks), text, min(u.page for u in current), max(u.page for u in current),
                                section(), estimate_tokens(text)))
        current = _overlap_tail(current, overlap)
        has_new = False

    for page in pages:
        blocks: list[Block] = page.blocks or ([Block("paragraph", page.text)] if page.text else [])
        for block in blocks:
            if block.kind == "heading":
                emit()
                current = []  # overlap never crosses a section boundary
                level = max(1, block.level)
                path[:] = [p for p in path if p[0] < level]
                path.append((level, block.text.strip()[:200]))
                current.append(_Unit(block.text.strip(), page.number, estimate_tokens(block.text)))
                continue
            units = (_table_units(block.text, page.number, target) if block.kind == "table"
                     else _split_long_text(block.text, page.number, target))
            for unit in units:
                used = sum(u.tokens for u in current)
                if has_new and used + unit.tokens > target:
                    emit()
                current.append(unit)
                has_new = True
    emit()
    return chunks
