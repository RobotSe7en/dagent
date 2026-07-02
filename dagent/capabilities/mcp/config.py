"""Configuration helpers for MCP providers."""

from __future__ import annotations

import os
import re
from typing import Mapping

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
DEFAULT_MCP_CONNECT_TIMEOUT_SECONDS = 60
DEFAULT_MCP_TOOL_TIMEOUT_SECONDS = 90
_SAFE_ENV_KEYS = {
    "ALLUSERSPROFILE",
    "APPDATA",
    "COMSPEC",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "USER",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}


def build_stdio_env(explicit_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a conservative stdio MCP subprocess environment."""

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper_key = key.upper()
        if upper_key in _SAFE_ENV_KEYS or upper_key.startswith("XDG_"):
            env[key] = value
    for key, value in (explicit_env or {}).items():
        env[str(key)] = _expand_env_refs(str(value))
    return env


def build_http_headers(explicit_headers: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build static HTTP headers for streamable HTTP MCP connections."""

    return {
        str(key): _expand_env_refs(str(value))
        for key, value in (explicit_headers or {}).items()
    }


def _expand_env_refs(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _ENV_REF.sub(replace, value)
