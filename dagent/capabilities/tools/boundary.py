"""Boundary enforcement for tool execution."""

from __future__ import annotations

from pathlib import Path
import re
import shlex

from dagent.schemas import Boundary


class BoundaryViolation(PermissionError):
    """Raised when a tool call exceeds the node boundary."""

    def __init__(
        self,
        message: str,
        *,
        tool_name: str | None = None,
        path: str | None = None,
        command: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.path = path
        self.command = command


class HardBoundaryViolation(BoundaryViolation):
    """Raised when a boundary failure must not be bypassed by review."""


_REDIRECTION_OPERATORS = {"<", "<<", ">", ">>", "1>", "1>>", "2>", "2>>", "&>"}
_REDIRECTION_PREFIX_RE = re.compile(r"^(?:[0-9]?>?>|&>|<<?)(.+)$")

_BLOCKED_SHELL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:"),
        "fork bomb",
    ),
    (
        re.compile(r"\bsudo\b(?=[^;&|`]*\s-S(?:\s|$))", re.IGNORECASE),
        "sudo password from stdin",
    ),
    (
        re.compile(
            r"\brm\b(?=[^;&|`]*-[^\s;&|`]*r)(?=[^;&|`]*-[^\s;&|`]*f)"
            r"[^;&|`]*(?:\s|^)(?:/|/\*|~|~/|\$HOME|\$\{HOME\})(?:\s|$)",
            re.IGNORECASE,
        ),
        "recursive delete of root or home",
    ),
    (
        re.compile(r"\brm\b[^;&|`]*--no-preserve-root\b", re.IGNORECASE),
        "disable rm root protection",
    ),
    (
        re.compile(r"(?:^|[;&|`\n])\s*(?:sudo\s+)?mkfs(?:\.[\w-]+)?\b", re.IGNORECASE),
        "format filesystem",
    ),
    (
        re.compile(
            r"(?:^|[;&|`\n])\s*(?:sudo\s+)?dd\b(?=[^;&|`]*\bof=/dev/"
            r"(?:sd|hd|vd|xvd|nvme|mmcblk|disk)\w*)",
            re.IGNORECASE,
        ),
        "write raw block device",
    ),
    (
        re.compile(
            r"(?:>|>>)\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|disk)\w*",
            re.IGNORECASE,
        ),
        "redirect to raw block device",
    ),
    (
        re.compile(
            r"(?:^|[;&|`\n])\s*(?:sudo\s+)?(?:shutdown|reboot|halt|poweroff)\b",
            re.IGNORECASE,
        ),
        "system shutdown or reboot",
    ),
    (
        re.compile(
            r"(?:^|[;&|`\n])\s*(?:sudo\s+)?systemctl\s+"
            r"(?:poweroff|reboot|halt|shutdown)\b",
            re.IGNORECASE,
        ),
        "system shutdown or reboot",
    ),
)


def enforce_command_allowed(command: str) -> None:
    if not command.strip():
        raise HardBoundaryViolation("Command cannot be empty.", command=command)

    blocked_reason = blocked_shell_command(command)
    if blocked_reason:
        raise HardBoundaryViolation(
            f"Command is blocked by shell safety policy: {blocked_reason}.",
            command=command,
        )


def enforce_command_paths_allowed(
    command: str,
    boundary: Boundary,
    workspace_root: Path,
    cwd: str | Path,
) -> None:
    """Reject explicit shell path tokens outside allowed roots.

    This is a conservative boundary check for obvious path arguments and
    redirections. It is not a shell sandbox; the hard command blacklist still
    handles destructive patterns separately.
    """
    cwd_path = _resolve_against_workspace(cwd, workspace_root)
    for raw_path in _explicit_command_paths(command):
        resolved_path = _resolve_against_cwd(raw_path, cwd_path)
        if not _path_allowed(resolved_path, boundary, workspace_root):
            allowed_display = ", ".join(
                str(_resolve_against_workspace(allowed_path, workspace_root))
                for allowed_path in (boundary.allowed_paths or ["."])
            )
            raise BoundaryViolation(
                f"Command path '{raw_path}' resolves outside allowed paths: "
                f"{resolved_path}. Allowed paths: {allowed_display}.",
                path=str(raw_path),
                command=command,
            )


def enforce_path_allowed(path: str | Path, boundary: Boundary, workspace_root: Path) -> Path:
    resolved_path = _resolve_against_workspace(path, workspace_root)
    if not _path_allowed(resolved_path, boundary, workspace_root):
        allowed_display = ", ".join(
            str(_resolve_against_workspace(allowed_path, workspace_root))
            for allowed_path in (boundary.allowed_paths or ["."])
        )
        raise BoundaryViolation(
            f"Path '{resolved_path}' is outside allowed paths: {allowed_display}.",
            path=str(path),
        )
    return resolved_path


def resolve_path_against_workspace(path: str | Path, workspace_root: Path) -> Path:
    return _resolve_against_workspace(path, workspace_root)


def _path_allowed(resolved_path: Path, boundary: Boundary, workspace_root: Path) -> bool:
    allowed_roots = boundary.allowed_paths or ["."]
    resolved_roots = [
        _resolve_against_workspace(allowed_path, workspace_root)
        for allowed_path in allowed_roots
    ]
    return any(_is_relative_to(resolved_path, root) for root in resolved_roots)


def _resolve_against_workspace(path: str | Path, workspace_root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = workspace_root / candidate
    return candidate.resolve()


def _resolve_against_cwd(path: str, cwd: Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _explicit_command_paths(command: str) -> list[str]:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return []
    paths: list[str] = []
    expect_redirect_path = False
    expect_command = True
    for token in tokens:
        if token in _REDIRECTION_OPERATORS:
            expect_redirect_path = True
            expect_command = False
            continue
        if token in {"|", "||", "&&", ";"}:
            expect_redirect_path = False
            expect_command = True
            continue
        if expect_command:
            if _looks_like_env_assignment(token):
                continue
            expect_command = False
            if not _command_token_should_be_checked(token):
                continue
        candidate = _path_from_token(token, force=expect_redirect_path)
        expect_redirect_path = False
        if candidate is not None:
            paths.append(candidate)
    return paths


def _looks_like_env_assignment(token: str) -> bool:
    name, separator, _value = token.partition("=")
    return bool(separator and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def _command_token_should_be_checked(token: str) -> bool:
    return token.startswith(("./", "../", "~"))


def _path_from_token(token: str, *, force: bool) -> str | None:
    if not token:
        return None
    match = _REDIRECTION_PREFIX_RE.match(token)
    if match is not None:
        token = match.group(1)
        force = True
    if "://" in token:
        return None
    if token.startswith("-") and "=" in token:
        _option, _separator, value = token.partition("=")
        return _path_from_token(value, force=True)
    if token.startswith("-") and not force:
        return None
    if force or token.startswith(("/", "./", "../", "~")) or "/" in token:
        return token
    return None


def blocked_shell_command(command: str) -> str | None:
    for pattern, reason in _BLOCKED_SHELL_PATTERNS:
        if pattern.search(command):
            return reason
    return None
