from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
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
_PROC_ROOT = Path("/proc")
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
        return {
            "TMPDIR": temporary,
            "TMP": temporary,
            "TEMP": temporary,
            # A sandboxed Node child can create an owner-only compile cache that
            # the parent Windows user cannot enumerate or remove afterward.
            "NODE_DISABLE_COMPILE_CACHE": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }


def current_temporary_root() -> Path | None:
    return _CURRENT_TEMPORARY_ROOT.get()


def _require_safe_cleanup_directory(path: Path, *, description: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise SafetyError(f"The ClawPatch Supervise {description} does not exist.") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise SafetyError(f"The ClawPatch Supervise {description} cannot be a symlink.")
    if not stat.S_ISDIR(metadata.st_mode):
        raise SafetyError(f"The ClawPatch Supervise {description} is not a directory.")
    if hasattr(os, "getuid"):
        if metadata.st_uid != os.getuid():
            raise SafetyError(
                f"The ClawPatch Supervise {description} is not owned by the current user."
            )
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise SafetyError(
                f"The ClawPatch Supervise {description} cannot be group or world writable."
            )


def _ensure_safe_cleanup_directory(
    path: Path,
    *,
    description: str,
    parents: bool = False,
) -> None:
    try:
        path.mkdir(mode=0o700, parents=parents)
    except FileExistsError:
        pass
    _require_safe_cleanup_directory(path, description=description)


def default_cleanup_root() -> Path:
    if hasattr(os, "getuid"):
        identity = str(os.getuid())
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if runtime:
            runtime_root = Path(runtime)
            if runtime_root.is_absolute():
                try:
                    _require_safe_cleanup_directory(
                        runtime_root,
                        description="per-user runtime directory",
                    )
                except SafetyError:
                    pass
                else:
                    return runtime_root / "clawpatch-supervise" / "runs"
    else:
        identity = hashlib.sha256(os.fsencode(str(Path.home()))).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"clawpatch-supervise-{identity}-private" / "runs"


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


def _lsof_path_has_live_reference(candidate: Path) -> bool | None:
    lsof = shutil.which("lsof")
    if lsof is None:
        return None
    try:
        result = subprocess.run(
            [lsof, "-nP", "-Fp", "+D", str(candidate.resolve())],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if any(line[1:].isdigit() for line in result.stdout.splitlines() if line.startswith("p")):
        return True
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return False
    return None


def _path_has_live_reference(candidate: Path) -> bool | None:
    if os.name != "posix":
        return False
    if not _PROC_ROOT.is_dir():
        return _lsof_path_has_live_reference(candidate)
    candidate_text = str(candidate.resolve())
    prefix = candidate_text + os.sep
    inspection_inconclusive = False
    try:
        processes = list(_PROC_ROOT.iterdir())
    except OSError:
        return None
    for process in processes:
        if not process.name.isdigit():
            continue
        links = [process / "cwd", process / "root", process / "exe"]
        descriptor_root = process / "fd"
        try:
            links.extend(descriptor_root.iterdir())
        except FileNotFoundError:
            continue
        except OSError:
            inspection_inconclusive = True
        for link in links:
            try:
                target = str(link.resolve())
            except FileNotFoundError:
                continue
            except (OSError, RuntimeError):
                inspection_inconclusive = True
                continue
            if target == candidate_text or target.startswith(prefix):
                return True
    return None if inspection_inconclusive else False


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
    using_default_root = root is None
    cleanup_root = (default_cleanup_root() if root is None else root).expanduser()
    if using_default_root and (cleanup_root.parent.exists() or cleanup_root.parent.is_symlink()):
        _require_safe_cleanup_directory(
            cleanup_root.parent,
            description="cleanup parent",
        )
    if not cleanup_root.exists():
        if cleanup_root.is_symlink():
            _require_safe_cleanup_directory(cleanup_root, description="cleanup root")
        return CleanupReport(cleanup_root, (), 0, 0)
    _require_safe_cleanup_directory(cleanup_root, description="cleanup root")
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
        if _pid_is_running(pid):
            entries.append(CleanupEntry(candidate, "ACTIVE", size))
            continue
        live_reference = _path_has_live_reference(candidate)
        if live_reference is None:
            entries.append(CleanupEntry(candidate, "UNSAFE", size))
            continue
        if live_reference:
            entries.append(CleanupEntry(candidate, "ACTIVE", size))
            continue
        if now() - float(created) < stale_after_seconds:
            entries.append(CleanupEntry(candidate, "RECENT", size))
            continue
        entries.append(CleanupEntry(candidate, "STALE", size))
        if apply:
            try:
                _remove_exact_owned_run(candidate, cleanup_root)
            except OSError:
                # Retain an owned stale run that Windows will not let this
                # process traverse. One blocked cache must not prevent cleanup
                # of other proven-owned runs or crash supervisor preflight.
                entries[-1] = CleanupEntry(candidate, "BLOCKED", size)
                continue
            removed += 1
            removed_bytes += size
    return CleanupReport(cleanup_root, tuple(entries), removed, removed_bytes)


def _remove_exact_owned_run(candidate: Path, cleanup_root: Path) -> None:
    if candidate.is_symlink() or not candidate.is_dir():
        raise SafetyError("The supervisor-owned run directory is no longer a safe directory.")
    if candidate.resolve().parent != cleanup_root.resolve() or _owned_marker(candidate) is None:
        raise SafetyError("The supervisor-owned run directory failed its ownership check.")
    live_reference = _path_has_live_reference(candidate)
    if live_reference is None:
        raise SafetyError(
            "Could not prove that no live process references the supervisor-owned run directory."
        )
    if live_reference:
        raise SafetyError("A live process still references the supervisor-owned run directory.")
    shutil.rmtree(candidate)


@contextmanager
def owned_run_directory(
    repo: Path,
    *,
    root: Path | None = None,
    on_blocked_cleanup: Callable[[Path, OSError], None] | None = None,
) -> Iterator[OwnedRunDirectory]:
    using_default_root = root is None
    cleanup_root = (default_cleanup_root() if root is None else root).expanduser()
    if using_default_root:
        _ensure_safe_cleanup_directory(
            cleanup_root.parent,
            description="cleanup parent",
        )
        _ensure_safe_cleanup_directory(cleanup_root, description="cleanup root")
    else:
        _ensure_safe_cleanup_directory(
            cleanup_root,
            description="cleanup root",
            parents=True,
        )
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
        except OSError as cleanup_error:
            if body_error is None:
                if on_blocked_cleanup is not None:
                    on_blocked_cleanup(candidate, cleanup_error)
                return
            body_error.add_note(
                f"ClawPatch Supervise cleanup also failed for its owned run directory: {candidate}"
            )
        except SafetyError as cleanup_error:
            if body_error is None:
                raise SafetyError(
                    f"ClawPatch Supervise could not remove its owned run directory: {candidate}"
                ) from cleanup_error
            body_error.add_note(
                f"ClawPatch Supervise cleanup also failed for its owned run directory: {candidate}"
            )
