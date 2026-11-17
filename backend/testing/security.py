"""Security test helpers: canary secrets and the export formula-injection oracle (SECURITY_ARCHITECTURE §7)."""

from __future__ import annotations

import csv
import io
import secrets
from dataclasses import dataclass, field

# Cells starting with one of these are interpreted as formulas by spreadsheet apps.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def escape_formula(value: str) -> str:
    """Reference implementation of the export rule: prefix dangerous cells with a single quote."""
    if isinstance(value, str) and value.startswith(FORMULA_TRIGGERS):
        return "'" + value
    return value


def is_dangerous_cell(value: str) -> bool:
    return isinstance(value, str) and value.startswith(FORMULA_TRIGGERS)


def unescaped_formula_cells(csv_text: str) -> list[tuple[int, int, str]]:
    """(row, col, value) of every exported cell a spreadsheet would evaluate. Must be empty for safe exports.

    Purely numeric negative values (e.g. "-12.5") are not formulas and are ignored.
    """
    bad = []
    for r, row in enumerate(csv.reader(io.StringIO(csv_text))):
        for c, cell in enumerate(row):
            if is_dangerous_cell(cell) and not _is_number(cell):
                bad.append((r, c, cell))
    return bad


def _is_number(s: str) -> bool:
    try:
        float(s)
    except ValueError:
        return False
    return True


@dataclass
class Canaries:
    """Unique, recognisable fake secrets. None of them may ever appear in logs, responses or audit rows."""

    tag: str = field(default_factory=lambda: secrets.token_hex(4))

    def __post_init__(self) -> None:
        t = self.tag
        self.db_password = f"CANARY-dbpw-{t}-Zq9"
        self.gemini_key = f"AIzaCANARY{t}gemini00000000000000000"
        self.api_key = f"nxk_cnry{t}_CANARYsecretCANARYsecret{t}"
        self.session_id = f"canarysession{t}0000000000000000"
        self.user_password = f"CANARY-userpw-{t}-Xx1!"
        self.kek_plain = f"CANARY-credential-{t}"

    def all(self) -> dict[str, str]:
        return {k: v for k, v in vars(self).items() if k != "tag"}

    def find_in(self, text: str) -> list[str]:
        return [name for name, value in self.all().items() if value and value in text]
