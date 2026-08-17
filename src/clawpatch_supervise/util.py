from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .errors import SafetyError


PUBLIC_COMMAND = "clawpatch-supervise"


_SECRET_PAIR_NAME_PATTERN = (
    r"(?:api[_-]?key|private[_-]?key|signing[_-]?key|token|password|passwd|passphrase|"
    r"secret|credentials?)"
)
_SECRET_NAME_PATTERN = rf"(?:{_SECRET_PAIR_NAME_PATTERN}|authorization)"
_SECRET_KEY_RE = re.compile(
    rf"(?:^|[_-]){_SECRET_NAME_PATTERN}(?:$|[_-])",
    re.IGNORECASE,
)
_CAMEL_CASE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]+")
_AUTHORIZATION_SCHEME_RE = re.compile(
    rf'''(?ix)
    (?P<prefix>["']?(?:[a-z0-9_-]*[_-])?authorization(?:[_-][a-z0-9_-]*)?["']?\s*[:=]\s*)
    (?:
        (?P<quoted>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
        |
        (?P<scheme>[^\s\r\n}}"']+)
        (?:(?P<separator>[ \t]+)(?P<credentials>[^\r\n]+))?
    )
    '''
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<kind>(?:[A-Z0-9]+ )*PRIVATE KEY)-----.*?"
    r"-----END (?P=kind)-----",
    re.DOTALL,
)
_URL_USERINFO_RE = re.compile(
    r"(?i)(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^/?#\s@]+@"
)
_SECRET_PAIR_RE = re.compile(
    rf'''(?ix)
    (?P<prefix>["']?(?:[a-z0-9_-]*[_-])?{_SECRET_PAIR_NAME_PATTERN}(?:[_-][a-z0-9_-]*)?["']?\s*[:=]\s*)
    (?P<value>
        "(?:\\.|[^"\\])*"
        |
        '(?:\\.|[^'\\])*'
        |
        [^\r\n,;}}&#"']+
    )
    '''
)
_SECRET_FLAG_RE = re.compile(
    rf"^--?(?:[a-z0-9_-]*[_-])?{_SECRET_NAME_PATTERN}(?:[_-][a-z0-9_-]*)?$",
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:/")


def _redact_scalar_text(text: str) -> str:
    redacted = _PEM_PRIVATE_KEY_RE.sub("<REDACTED>", text)
    redacted = _URL_USERINFO_RE.sub(
        lambda match: f"{match.group('scheme')}<REDACTED>@",
        redacted,
    )
    redacted = _AUTHORIZATION_SCHEME_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('scheme')}"
            f"{match.group('separator')}<REDACTED>"
            if match.group("credentials") is not None
            else f"{match.group('prefix')}<REDACTED>"
        ),
        redacted,
    )
    redacted = _BEARER_RE.sub("Bearer <REDACTED>", redacted)
    return _SECRET_PAIR_RE.sub(lambda match: f"{match.group('prefix')}<REDACTED>", redacted)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{PUBLIC_COMMAND}-{stamp}-{os.urandom(3).hex()}"


def slugify(value: str, max_length: int = 64) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (value or "item")[:max_length].rstrip("-")


def canonical_json_bytes(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(data: Any) -> str:
    return sha256_bytes(canonical_json_bytes(data))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _redact_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<REDACTED>"
            if _SECRET_KEY_RE.search(_CAMEL_CASE_BOUNDARY_RE.sub("_", str(key)))
            else _redact_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, str):
        return _redact_scalar_text(value)
    return value


def redact_text(text: str | bytes | None) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    stripped = text.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            redacted_json = json.dumps(
                _redact_json_value(parsed),
                ensure_ascii=False,
                separators=(",", ":") if "\n" not in text else None,
                indent=2 if "\n" in text else None,
            )
            prefix = text[: len(text) - len(text.lstrip())]
            suffix = text[len(text.rstrip()) :]
            return prefix + redacted_json + suffix

    return _redact_scalar_text(text)


def redact_argv(argv: Sequence[str]) -> list[str]:
    """Return log-safe argv, including split ``--token VALUE`` style secrets."""
    redacted: list[str] = []
    redact_next = False
    for raw in argv:
        item = str(raw)
        if redact_next:
            redacted.append("<REDACTED>")
            redact_next = False
            continue
        if "=" in item:
            flag, value = item.split("=", 1)
            if _SECRET_FLAG_RE.fullmatch(flag):
                redacted.append(f"{flag}=<REDACTED>")
                continue
        if _SECRET_FLAG_RE.fullmatch(item):
            redacted.append(item)
            redact_next = True
            continue
        redacted.append(redact_text(item))
    return redacted


def _fsync_parent_directory(path: Path) -> None:
    """Persist a replaced directory entry where directory descriptors are supported."""
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    directory_fd = os.open(path.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_parent_directory(path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_within(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    candidate = candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SafetyError(f"Path escapes allowed root: {candidate}") from exc
    return candidate


def safe_repo_relative(value: str) -> str:
    normalized = str(value)
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or "\\" in normalized
        or normalized.startswith("/")
        or _WINDOWS_ABSOLUTE_RE.match(normalized)
        or pure.is_absolute()
        or ".." in pure.parts
    ):
        raise SafetyError(f"Unsafe repository-relative path: {value!r}")
    return str(pure)


def file_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def copy_file_preserving_mode(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    destination.chmod(file_mode(source))
