"""Error helpers for MCP capabilities."""

from __future__ import annotations

import re

_SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9_]{12,}"),
    re.compile(r"(?i)(token|api[_-]?key|authorization)=([^\s&]+)"),
]


def sanitize_error(error: object) -> str:
    text = str(error)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redacted, text)
    return text


def _redacted(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 2:
        return f"{match.group(1)}=<redacted>"
    if match.group(0).lower().startswith("bearer "):
        return "Bearer <redacted>"
    return "<redacted>"
