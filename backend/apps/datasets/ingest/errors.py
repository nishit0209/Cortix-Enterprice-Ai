from __future__ import annotations


class IngestError(Exception):
    """A file/data problem with a stable, user-facing error code (never a stack trace)."""

    def __init__(self, code: str, message: str, *, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
