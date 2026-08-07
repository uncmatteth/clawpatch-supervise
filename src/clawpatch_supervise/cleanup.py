from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from .errors import SafetyError

OWNERSHIP_MARKER = ".clawpatch-supervise-owned.json"
OWNERSHIP_SCHEMA = 1
DEFAULT_STALE_AFTER_SECONDS = 60 * 60
_CURRENT_TEMPORARY_ROOT: ContextVar[Path | None] = ContextVar(
    "clawpatch_supervise_temporary_root", default=None
)


@dataclass(frozen=True)
class CleanupEntry:
    path: Path
    status: str
    bytes: int


@dataclass(frozen=True)
class CleanupReport:
    root: Path
    entries: tuple[CleanupEntry, ...]
    removed: int
    removed_bytes: int


@dataclass(frozen=True)
class OwnedRunDirectory:
    path: Path
    temporary_root: Path

    def child_environment(self) -> dict[str, str]:
        temporary = str(self.temporary_root)
        return {"TMPDIR": temporary, "TMP": temporary, "TEMP": temporary}


def current_temporary_root() -> Path | None:
    return _CURRENT_TEMPORARY_ROOT.get()


def default_cleanup_root() -> Path:
    if hasattr(os, "getuid"):
        identity = str(os.getuid())
    else:
        identity = hashlib.sha256(os.fsencode(str(Path.home()))).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"clawpatch-supervise-{identity}" / "runs"


def _windows_pid_is_running(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    error_invalid_parameter = 87
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    handle = open_process(process_query_limited_information, False, pid)
    if handle:
        close_handle(handle)
        return True
    # Access-denied and other unknown errors preserve the directory. Windows
    # reports a nonexistent PID as ERROR_INVALID_PARAMETER.
    return ctypes.get_last_error() != error_invalid_parameter


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _directory_bytes(path: Path) -> int:
    total = 0
    for root, _directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in files:
            candidate = root_path / name
            try:
                if not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    return total


def _path_has_live_reference(candidate: Path) -> bool:
    proc_root = Path("/proc")
    if os.name != "posix" or not proc_root.is_dir():
        return False
    candidate_text = str(candidate.resolve())
    prefix = candidate_text + os.sep
    for process in proc_root.iterdir():
        if not process.name.isdigit():
            continue
        links = [process / "cwd", process / "root", process / "exe"]
        descriptor_root = process / "fd"
        try:
            links.extend(descriptor_root.iterdir())
        except OSError:
            pass
        for link in links:
            try:
                target = str(link.resolve())
            except (OSError, RuntimeError):
                continue
            if target == candidate_text or target.startswith(prefix):
                return True
    return False


def _owned_marker(candidate: Path) -> dict[str, object] | None:
    marker = candidate / OWNERSHIP_MARKER
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if (
        payload.get("schema") != OWNERSHIP_SCHEMA
        or payload.get("owner") != "clawpatch-supervise"
        or payload.get("kind") != "run-temp"
        or payload.get("directory") != candidate.name
    ):
        return None
    return payload


def cleanup_owned_runs(
    *,
    apply: bool,
    root: Path | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: Callable[[], float] = time.time,
) -> CleanupReport:
    cleanup_root = (root or default_cleanup_root()).expanduser()
    if cleanup_root.is_symlink():
        raise SafetyError("The ClawPatch Supervise cleanup root cannot be a symlink.")
    if not cleanup_root.exists():
        return CleanupReport(cleanup_root, (), 0, 0)
    if not cleanup_root.is_dir():
        raise SafetyError("The ClawPatch Supervise cleanup root is not a directory.")
    resolved_root = cleanup_root.resolve()
    entries: list[CleanupEntry] = []
    removed = 0
    removed_bytes = 0
    for candidate in sorted(cleanup_root.iterdir(), key=lambda item: item.name):
        if candidate.is_symlink() or not candidate.is_dir():
            entries.append(CleanupEntry(candidate, "UNOWNED", 0))
            continue
        if candidate.resolve().parent != resolved_root:
            entries.append(CleanupEntry(candidate, "UNSAFE", 0))
            continue
        marker = _owned_marker(candidate)
        size = _directory_bytes(candidate)
        if marker is None:
            entries.append(CleanupEntry(candidate, "UNOWNED", size))
            continue
        pid = marker.get("pid")
        created = marker.get("created_unix")
        if isinstance(pid, bool) or not isinstance(pid, int):
            entries.append(CleanupEntry(candidate, "UNSAFE", size))
            continue
        if isinstance(created, bool) or not isinstance(created, (int, float)):
            entries.append(CleanupEntry(candidate, "UNSAFE", size))
            continue
        if _pid_is_running(pid) or _path_has_live_reference(candidate):
            entries.append(CleanupEntry(candidate, "ACTIVE", size))
            continue
        if now() - float(created) < stale_after_seconds:
            entries.append(CleanupEntry(candidate, "RECENT", size))
            continue
        entries.append(CleanupEntry(candidate, "STALE", size))
        if apply:
            _remove_exact_owned_run(candidate, cleanup_root)
            removed += 1
            removed_bytes += size
    return CleanupReport(cleanup_root, tuple(entries), removed, removed_bytes)


def _remove_exact_owned_run(candidate: Path, cleanup_root: Path) -> None:
    if candidate.is_symlink() or not candidate.is_dir():
        raise SafetyError("The supervisor-owned run directory is no longer a safe directory.")
    if candidate.resolve().parent != cleanup_root.resolve() or _owned_marker(candidate) is None:
        raise SafetyError("The supervisor-owned run directory failed its ownership check.")
    if _path_has_live_reference(candidate):
        raise SafetyError("A live process still references the supervisor-owned run directory.")
    shutil.rmtree(candidate)


@contextmanager
def owned_run_directory(
    repo: Path,
    *,
    root: Path | None = None,
) -> Iterator[OwnedRunDirectory]:
    cleanup_root = (root or default_cleanup_root()).expanduser()
    if cleanup_root.is_symlink():
        raise SafetyError("The ClawPatch Supervise cleanup root cannot be a symlink.")
    cleanup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not cleanup_root.is_dir():
        raise SafetyError("The ClawPatch Supervise cleanup root is not a directory.")
    cleanup_owned_runs(apply=True, root=cleanup_root)
    created = time.time()
    nonce = secrets.token_hex(12)
    candidate = cleanup_root / f"run-{nonce}"
    candidate.mkdir(mode=0o700)
    marker = {
        "schema": OWNERSHIP_SCHEMA,
        "owner": "clawpatch-supervise",
        "kind": "run-temp",
        "directory": candidate.name,
        "pid": os.getpid(),
        "created_unix": created,
        "repo": str(repo.expanduser().resolve()),
    }
    marker_path = candidate / OWNERSHIP_MARKER
    marker_path.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    temporary_root = candidate / "tmp"
    temporary_root.mkdir(mode=0o700)
    token = _CURRENT_TEMPORARY_ROOT.set(temporary_root)
    body_error: BaseException | None = None
    try:
        yield OwnedRunDirectory(candidate, temporary_root)
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        _CURRENT_TEMPORARY_ROOT.reset(token)
        try:
            _remove_exact_owned_run(candidate, cleanup_root)
        except (OSError, SafetyError) as cleanup_error:
            if body_error is None:
                raise SafetyError(
                    f"ClawPatch Supervise could not remove its owned run directory: {candidate}"
                ) from cleanup_error
            body_error.add_note(
                f"ClawPatch Supervise cleanup also failed for its owned run directory: {candidate}"
            )
