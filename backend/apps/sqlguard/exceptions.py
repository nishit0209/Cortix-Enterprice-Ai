"""Errors raised by apps.sqlguard. Every `code` is one of nexora.contracts.GUARD_REJECT_CODES."""

from __future__ import annotations

from apps.core.exceptions import SqlRejected
from nexora.contracts import GUARD_REJECT_CODES


class GuardReject(Exception):
    """Internal control flow of validate(): becomes GuardResult(ok=False, reject_code=code)."""

    def __init__(self, code: str, message: str):
        if code not in GUARD_REJECT_CODES:  # pragma: no cover - programming error, fail closed
            code, message = "invalid_sql", f"internal guard error ({message})"
        super().__init__(message)
        self.code = code
        self.message = message


class GuardError(SqlRejected):
    """Raised by execute(): problem+json with `code` in GUARD_REJECT_CODES (422 unless stated)."""

    status_code = 422
    default_code = "execution_error"
    default_detail = "The query could not be executed."

    def __init__(self, detail: str | None = None, *, code: str | None = None, status: int | None = None,
                 extra: dict | None = None):
        code = code or self.default_code
        if code not in GUARD_REJECT_CODES:  # pragma: no cover - fail closed on programming errors
            code = "execution_error"
        super().__init__(detail, code=code, extra=extra)
        if status is not None:
            self.status_code = status

    @property
    def reject_code(self) -> str:
        codes = self.get_codes()
        return codes if isinstance(codes, str) else "execution_error"


class GuardTimeout(GuardError):
    status_code = 504
    default_code = "timeout"
    default_detail = "The query exceeded its time limit."
