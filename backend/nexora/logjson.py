"""Minimal JSON log formatter for production (stdlib only)."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

_STD = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():  # extra=... fields (request_id, org_id, ...)
            if key not in _STD and not key.startswith("_"):
                try:
                    json.dumps(value)
                    out[key] = value
                except TypeError:
                    out[key] = repr(value)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)
