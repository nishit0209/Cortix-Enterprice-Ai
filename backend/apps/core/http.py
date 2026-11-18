"""Small HTTP helpers shared by views and storage."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import quote

_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\-]")


def safe_filename(filename: str, default: str = "download") -> str:
    """Strip path separators and control characters from a user-supplied file name (keeps Unicode)."""
    name = unicodedata.normalize("NFC", str(filename or ""))
    name = name.replace("\\", "_").replace("/", "_")
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")  # Cc/Cf/Cs/Co/Cn: CR, LF, NUL, bidi
    name = name.strip().strip(".").strip()
    return name[:200] or default


def content_disposition(filename: str, disposition: str = "attachment") -> str:
    """RFC 6266 / RFC 5987 header value: a sanitised quoted ASCII fallback plus `filename*=UTF-8''<pct>`.

    The fallback keeps only [A-Za-z0-9 ._()-], so quotes, ';', '=', '*', CR and LF can never inject
    header parameters; the extended value is fully percent-encoded."""
    if disposition not in ("attachment", "inline"):
        raise ValueError("disposition must be 'attachment' or 'inline'")
    name = safe_filename(filename)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = _ASCII_UNSAFE.sub("_", ascii_name).strip() or "download"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
