from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterator

from .clawpatch_protocol import (
    ClawpatchFailure,
    ClawpatchFailureKind,
    RepairAction,
    classify_clawpatch_failure,
    decide_repair_transition,
    failure_from_legacy_outcome,
)
from .cleanup import current_temporary_root
from .errors import GateFailure, SafetyError
from .runner import CommandRunner
from .util import atomic_write_json, utc_now

PROJECT_DIR = ".manageroo"
MINIMUM_CLAWPATCH_VERSION = (0, 7, 2)
CLAWPATCH_CHILD_WATCHDOG_SECONDS = 900
CLAWPATCH_ZERO_SOURCE_RETRY_LIMIT = 2
RELEASE_PROGRESS_VERSION = 6
LIFECYCLE = (
    "repository/process/Git preflight -> exact-owned disposable validation-service setup when the "
    "repository declares a supported contract -> clawpatch status --json -> stale-lock cleanup when proven -> "
    "configured repository baseline gates when present -> clawpatch map -> complete review of every "
    "pending feature in bounded ClawPatch worker waves with an exact decreasing-pending proof -> "
    "clawpatch next/show -> same-finding fix iterations while each produces a new "
    "source tree -> local-only exact-path temporary commit for partial progress -> configured project "
    "gates when present; a red gate on a provenance-verified stopped open or uncertain repair "
    "reenters only that finding with the exact gate evidence -> exact fixed revalidation "
    "(with bounded read-only, workspace-write, and external trusted-host validation transitions) -> "
    "one combined exact-path final commit/push when authorized -> an open revalidation with source "
    "progress amends the local iteration and reenters the same finding without a cap; an open "
    "revalidation without source progress informs up to two additional fix attempts; no-progress, "
    "unsupported or failed transitions stop with source changes intact; strict mode keeps uncertain "
    "repairs in the same-finding loop, while external unattended mode commits any exact applied repair, "
    "retains the uncertain classification, and continues the open queue; a ClawPatch-owned false-positive "
    "restores only its exact supervisor-owned repair paths to the finding start tree, retires "
    "the checkpoint, and advances; any ownership mismatch stops unchanged -> "
    "final open/uncertain closure -> rebuild generated ClawPatch state and repeat map plus complete "
    "review at the new HEAD after every nonempty generation -> COMPLETE only after a fresh full "
    "review generation finds zero findings; untracked node_modules install output remains runtime "
    "noise while tracked dependency files remain source; a repeated non-clean source tree stops "
    "as nonconvergent"
)
_FINDING_ID = re.compile(r"^fnd_[A-Za-z0-9_.-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RELEASE_CHILD_INHERITED_ENV_NAMES = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERPROFILE",
        "WINDIR",
    }
)
_CLAWPATCH_POLICY_ENV_NAMES = frozenset(
    {
        "CLAWPATCH_CODEX_SANDBOX",
        "CLAWPATCH_CODEX_TIMEOUT_MS",
        "MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK",
        "MANAGEROO_CLAWPATCH_CHILD_TIMEOUT_SECONDS",
    }
)
_SUPERVISOR_UPGRADE_PATHS = frozenset(
    {
        "AGENTS.md",
        "BUILD-VALIDATION.json",
        "README.md",
        "pyproject.toml",
        "docs/ARCHITECTURE.md",
        "docs/ENFORCEMENT_MATRIX.md",
        "docs/EXTERNAL_INTEGRATIONS.md",
        "docs/LIMITATIONS.md",
        "docs/SOLO_OPERATOR_MODE.md",
        "src/manageroo/clawpatch_external.py",
        "src/manageroo/clawpatch_protocol.py",
        "src/manageroo/clawpatch_release.py",
        "src/manageroo/runner.py",
        "src/manageroo/validation_services.py",
        "tests/test_clawpatch_release_sweep.py",
        "tests/test_clawpatch_protocol.py",
        "tests/test_disposable_validation_services.py",
        "tests/test_external_clawpatch_supervisor.py",
        "tests/test_final_clawpatch_regressions.py",
        "src/clawpatch_supervise/clawpatch_external.py",
        "src/clawpatch_supervise/clawpatch_protocol.py",
        "src/clawpatch_supervise/clawpatch_release.py",
        "src/clawpatch_supervise/runner.py",
        "src/clawpatch_supervise/validation_services.py",
        "tests/test_cli.py",
        "tests/test_partial_progress.py",
        "tests/test_protocol.py",
        "tests/test_release.py",
    }
)


class _UnresolvedFinding(SafetyError):
    def __init__(
        self,
        message: str,
        *,
        finding_id: str,
        outcome: str | None = None,
        failure: ClawpatchFailure | None = None,
        repair_action: RepairAction | None = None,
    ) -> None:
        super().__init__(message)
        self.finding_id = finding_id
        self.outcome = outcome
        self.failure = failure
        self.repair_action = repair_action


class ClawpatchCommandFailure(SafetyError):
    def __init__(self, message: str, *, failure: ClawpatchFailure) -> None:
        super().__init__(message)
        self.failure = failure
        self.returncode = failure.exit_code


_ClawpatchCommandFailure = ClawpatchCommandFailure


class ClawpatchStop(SafetyError):
    def __init__(self, message: str, *, repair_action: RepairAction) -> None:
        super().__init__(message)
        self.repair_action = repair_action


class _MissingFinding(SafetyError):
    def __init__(self, message: str, *, finding_id: str) -> None:
        super().__init__(message)
        self.finding_id = finding_id


def _release_clawpatch_env(
    *,
    trusted_host_codex_sandbox_bypass: bool,
    allow_sandbox_bypass_fallback: bool = False,
    child_timeout_seconds: int = CLAWPATCH_CHILD_WATCHDOG_SECONDS,
    child_env_overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    if child_timeout_seconds < 60:
        raise SafetyError("Clawpatch child timeout must be at least 60 seconds.")
    child_env = {
        name: value
        for name in _RELEASE_CHILD_INHERITED_ENV_NAMES
        if (value := os.environ.get(name)) is not None
    }
    overrides = child_env_overrides or {}
    child_env["CLAWPATCH_CODEX_TIMEOUT_MS"] = str(child_timeout_seconds * 1_000)
    child_env["MANAGEROO_CLAWPATCH_CHILD_TIMEOUT_SECONDS"] = str(child_timeout_seconds)
    child_env.pop("MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK", None)
    child_env.pop("CLAWPATCH_CODEX_SANDBOX", None)
    if trusted_host_codex_sandbox_bypass:
        child_env["CLAWPATCH_CODEX_SANDBOX"] = "bypass"
    elif allow_sandbox_bypass_fallback:
        child_env["MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK"] = "1"
    for name, value in overrides.items():
        if not _ENV_NAME.fullmatch(name) or "\x00" in value:
            raise SafetyError(
                "The supervisor received an invalid validation-service environment value."
            )
        if name.upper() in _CLAWPATCH_POLICY_ENV_NAMES:
            raise SafetyError(
                f"Validation services cannot override policy-owned supervisor variable {name}."
            )
        child_env[name] = value
    return child_env


def _child_timeout_seconds(env: dict[str, str]) -> int:
    raw = env.get("MANAGEROO_CLAWPATCH_CHILD_TIMEOUT_SECONDS")
    if raw is None:
        return CLAWPATCH_CHILD_WATCHDOG_SECONDS
    try:
        value = int(raw)
    except ValueError as exc:
        raise SafetyError("The supervisor received an invalid ClawPatch child timeout.") from exc
    if value < 60:
        raise SafetyError("Clawpatch child timeout must be at least 60 seconds.")
    return value


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 1800,
    env: dict[str, str] | None = None,
    kill_process_group: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = _platform_command(argv, platform_name=os.name)
    if kill_process_group:
        result = CommandRunner().run(
            command,
            cwd=cwd,
            timeout_seconds=timeout,
            env=env,
            kill_process_group=True,
        )
        output = result.stdout
        if result.stderr:
            output = output + ("\n" if output else "") + result.stderr
        if result.timed_out:
            output = output + ("\n" if output else "") + "TIMEOUT"
        return subprocess.CompletedProcess(command, result.exit_code, output, None)
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout if isinstance(exc.stdout, str) else ""
        return subprocess.CompletedProcess(command, 124, output + "\nTIMEOUT", None)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, str(exc), None)


def _platform_command(argv: list[str], *, platform_name: str) -> list[str]:
    command = list(argv)
    if platform_name == "nt" and command:
        executable = PureWindowsPath(command[0]).name.lower()
        if executable in {"clawpatch", "clawpatch.exe", "clawpatch.cmd", "clawpatch.bat"}:
            resolved = shutil.which(command[0]) or shutil.which("clawpatch")
            if resolved:
                command[0] = resolved
    return command


def _must_run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 1800,
    env: dict[str, str] | None = None,
) -> str:
    result = _run(argv, cwd=cwd, timeout=timeout, env=env)
    if result.returncode:
        raise SafetyError(
            f"command: {shlex.join(argv)}\nexit code: {result.returncode}\n"
            f"failed requirement: command must exit 0\noutput:\n{result.stdout[-6000:]}"
        )
    return result.stdout


def _parse_json_output(output: str, *, command: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as exc:
        value = None
        decoder = json.JSONDecoder()
        candidates: list[dict[str, Any]] = []
        for match in re.finditer(r"(?m)^[ \t]*(\{)", output):
            try:
                candidate, _end = decoder.raw_decode(output, match.start(1))
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                candidates.append(candidate)
        if len(candidates) == 1:
            value = candidates[0]
        elif len(candidates) > 1:
            raise SafetyError(
                f"Clawpatch {command} returned multiple ambiguous JSON objects:\n{output[-4000:]}"
            ) from exc
        if value is None:
            raise SafetyError(
                f"Clawpatch {command} did not return valid JSON:\n{output[-4000:]}"
            ) from exc
    if not isinstance(value, dict):
        raise SafetyError(f"Clawpatch {command} returned an unexpected JSON value.")
    return value


def _git_root(repo: Path) -> Path:
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=repo, timeout=30)
    if result.returncode or not result.stdout.strip():
        raise SafetyError("Clawpatch release sweep requires an existing Git repository.")
    return Path(result.stdout.strip()).resolve()


def _git_text(repo: Path, argv: list[str]) -> str:
    return _must_run(argv, cwd=repo, timeout=600).strip()


def _require_branch(repo: Path, expected: str, *, phase: str) -> None:
    current = _git_text(repo, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if current != expected:
        raise SafetyError(
            f"Git branch changed during Clawpatch {phase}; expected {expected!r}, found {current!r}."
        )


def _require_synchronized_remote_branch(
    repo: Path,
    branch: str,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    if branch == "HEAD":
        raise SafetyError("Clawpatch release sweep cannot synchronize a detached HEAD.")
    _require_branch(repo, branch, phase="remote synchronization")
    _must_run(["git", "remote", "get-url", "origin"], cwd=repo, timeout=60)
    local = _git_text(repo, ["git", "rev-parse", "HEAD"])
    remote_line = _git_text(repo, ["git", "ls-remote", "origin", f"refs/heads/{branch}"])
    remote = remote_line.split()[0] if remote_line else ""
    if not remote:
        raise SafetyError(f"Origin has no branch refs/heads/{branch}; synchronization is unproven.")
    if remote != local:
        if _source_paths(repo):
            raise SafetyError(
                f"Local HEAD {local!r} is not synchronized with origin/{branch} at {remote!r}."
            )
        _must_run(
            ["git", "fetch", "--no-tags", "origin", f"refs/heads/{branch}"],
            cwd=repo,
            timeout=300,
        )
        fetched = _git_text(repo, ["git", "rev-parse", "FETCH_HEAD"])
        live_remote_line = _git_text(
            repo, ["git", "ls-remote", "origin", f"refs/heads/{branch}"]
        )
        live_remote = live_remote_line.split()[0] if live_remote_line else ""
        if fetched != remote or live_remote != remote:
            raise SafetyError(
                f"Origin/{branch} changed during synchronization; rerun the supervisor."
            )
        ancestor = _run(
            ["git", "merge-base", "--is-ancestor", local, remote],
            cwd=repo,
            timeout=60,
        )
        if ancestor.returncode:
            raise SafetyError(
                f"Local HEAD {local!r} is not synchronized with origin/{branch} at {remote!r}."
            )
        if progress is not None:
            progress(
                {
                    "phase": "git-sync",
                    "current": "?",
                    "total": "?",
                    "command": f"git merge --ff-only {remote}",
                    "detail": f"fast-forward local {branch} to origin/{branch}",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
        _require_branch(repo, branch, phase="remote synchronization")
        with tempfile.TemporaryDirectory(prefix="clawpatch-supervise-empty-hooks-") as hooks:
            _must_run(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={hooks}",
                    "merge",
                    "--ff-only",
                    remote,
                ],
                cwd=repo,
                timeout=300,
            )
        _require_branch(repo, branch, phase="remote synchronization")
        local = _git_text(repo, ["git", "rev-parse", "HEAD"])
        final_remote_line = _git_text(
            repo, ["git", "ls-remote", "origin", f"refs/heads/{branch}"]
        )
        final_remote = final_remote_line.split()[0] if final_remote_line else ""
        if local != remote or final_remote != remote:
            raise SafetyError(
                f"Local HEAD {local!r} is not synchronized with origin/{branch} at "
                f"{final_remote!r}."
            )
    return local


def _status_entries(repo: Path) -> list[tuple[str, str]]:
    output = _must_run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--no-renames", "-z"],
        cwd=repo,
        timeout=120,
    )
    entries: list[tuple[str, str]] = []
    for record in output.split("\0"):
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise SafetyError("Git returned malformed status output.")
        entries.append((record[:2], record[3:]))
    return sorted(set(entries), key=lambda entry: (entry[1], entry[0]))


def _status_paths(repo: Path) -> list[str]:
    return sorted({path for _status, path in _status_entries(repo)})


def _is_untracked_dependency_path(status: str, path: str) -> bool:
    return status == "??" and "node_modules" in PurePosixPath(path).parts


def _source_paths(repo: Path) -> list[str]:
    return sorted(
        {
            path
            for status, path in _status_entries(repo)
            if path != ".clawpatch"
            and not path.startswith(".clawpatch/")
            and not _is_untracked_dependency_path(status, path)
        }
    )


def _normalized_stopped_owned_paths(
    repo: Path,
    checkpoint: dict[str, Any],
    recorded_paths: list[str],
) -> list[str]:
    current_source = _source_paths(repo)
    if recorded_paths == current_source:
        return recorded_paths
    entries = _status_entries(repo)
    legacy_source = sorted(
        {
            path
            for _status, path in entries
            if path != ".clawpatch" and not path.startswith(".clawpatch/")
        }
    )
    removed_paths = sorted(set(recorded_paths) - set(current_source))
    status_by_path = {path: status for status, path in entries}
    recorded_fingerprint = str(checkpoint.get("owned_source_fingerprint", ""))
    if (
        not current_source
        or not removed_paths
        or not set(current_source).issubset(recorded_paths)
        or recorded_paths != legacy_source
        or any(
            not _is_untracked_dependency_path(status_by_path.get(path, ""), path)
            for path in removed_paths
        )
        or not recorded_fingerprint
        or _source_paths_fingerprint(repo, recorded_paths) != recorded_fingerprint
    ):
        return recorded_paths
    return current_source


def _gitlink_paths(repo: Path) -> list[str]:
    output = _must_run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=repo,
        timeout=120,
    )
    paths: list[str] = []
    for record in output.split("\0"):
        if not record:
            continue
        metadata, separator, path = record.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise SafetyError("Git returned malformed staged-file metadata.")
        if fields[0] == "160000":
            paths.append(path)
    return sorted(set(paths))


def _command_name(value: str) -> str:
    return value.replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _is_clawpatch_argv(argv: list[str]) -> bool:
    if not argv:
        return False
    commands = {
        "clawpatch",
        "clawpatch.exe",
        "clawpatch.cmd",
        "clawpatch.bat",
        "clawpatch-supervise",
        "clawpatch-supervise.exe",
    }
    first = _command_name(argv[0])
    if first in commands:
        return True
    interpreters = {
        "node",
        "node.exe",
        "bun",
        "bun.exe",
        "python",
        "python.exe",
        "python3",
        "python3.exe",
    }
    if first not in interpreters:
        return False
    if len(argv) >= 3 and argv[1] == "-m":
        return argv[2] in {
            "clawpatch_supervise",
            "clawpatch_supervise.clawpatch_external",
            "manageroo.clawpatch_external",
        }
    script = next((value for value in argv[1:4] if not value.startswith("-")), "")
    return _command_name(script) in commands


def _process_repository_root(cwd: Path) -> Path | None:
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=30)
    if result.returncode or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _active_clawpatch_processes(repo: Path) -> list[dict[str, Any]]:
    root = repo.resolve()
    found: list[dict[str, Any]] = []
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                argv = [
                    value.decode("utf-8", "replace")
                    for value in (entry / "cmdline").read_bytes().split(b"\0")
                    if value
                ]
                if not _is_clawpatch_argv(argv):
                    continue
                cmdline = " ".join(argv)
                cwd = (entry / "cwd").resolve()
            except (FileNotFoundError, PermissionError, OSError):
                continue
            if _process_repository_root(cwd) == root:
                found.append({"pid": int(entry.name), "cwd": str(cwd), "command": cmdline.strip()})
        return found
    if os.name == "nt":
        return _windows_clawpatch_processes(root)
    result = _run(["ps", "-eo", "pid=,command="], cwd=root, timeout=30)
    if result.returncode:
        raise SafetyError("Could not prove that no other Clawpatch process is active.")
    for line in result.stdout.splitlines():
        process = line.strip().split(maxsplit=1)
        if len(process) != 2:
            continue
        try:
            pid = int(process[0])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        command = process[1]
        try:
            argv = shlex.split(command)
        except ValueError:
            continue
        if not _is_clawpatch_argv(argv):
            continue
        cwd = _unix_process_cwd(pid, root)
        if cwd is not None and _process_repository_root(cwd) == root:
            found.append({"pid": pid, "cwd": str(cwd), "command": command})
    return found


def _unix_process_cwd(pid: int, root: Path) -> Path | None:
    lsof = shutil.which("lsof")
    if lsof is None:
        raise SafetyError("Could not inspect Clawpatch process working directories on Unix.")
    result = _run([lsof, "-a", "-p", str(pid), "-d", "cwd", "-Fn"], cwd=root, timeout=30)
    paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]
    if not result.returncode and len(paths) == 1:
        return Path(paths[0]).resolve()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except OSError:
        pass
    raise SafetyError(f"Could not establish repository ownership for Clawpatch process {pid}.")


def _windows_clawpatch_processes(root: Path) -> list[dict[str, Any]]:
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        raise SafetyError("Could not inspect live Clawpatch processes on Windows.")
    script = (
        "$rows = Get-CimInstance Win32_Process | Where-Object { "
        "$_.ProcessId -ne $PID -and $_.CommandLine -and "
        "$_.CommandLine -match '(?i)(^|[\\\\/\\s])clawpatch(?:\\.cmd|\\.exe|\\.js)?(?:\\s|$)' }; "
        "$rows | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    result = _run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=root,
        timeout=30,
    )
    if result.returncode:
        raise SafetyError("Could not inspect live Clawpatch processes on Windows.")
    if not result.stdout.strip():
        return []
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SafetyError("Windows returned malformed Clawpatch process data.") from exc
    rows = parsed if isinstance(parsed, list) else [parsed]
    found: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise SafetyError("Windows returned malformed Clawpatch process data.")
        found.append(
            {
                "pid": row.get("ProcessId"),
                "cwd": "unknown; Windows process inspection is conservative",
                "command": str(row.get("CommandLine") or "").strip(),
            }
        )
    return found


def _require_no_process(repo: Path) -> None:
    active = _active_clawpatch_processes(repo)
    if active:
        raise SafetyError(f"A Clawpatch process is already active for this repository: {active}")


@contextmanager
def _release_sweep_lock(repo: Path) -> Iterator[None]:
    raw_path = _git_text(
        repo,
        ["git", "rev-parse", "--git-path", "manageroo-clawpatch-release.lock"],
    )
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = repo / candidate
    lock_path = candidate.parent.resolve() / candidate.name
    flags = os.O_CREAT | os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise SafetyError(
            f"Could not open the Clawpatch release sweep lock: {lock_path}: {exc}"
        ) from exc
    acquired = False
    try:
        lock_state = os.fstat(descriptor)
        path_state = lock_path.lstat()
        reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        file_attributes = getattr(path_state, "st_file_attributes", 0)
        if (
            stat.S_ISLNK(path_state.st_mode)
            or (reparse_point and file_attributes & reparse_point)
            or not stat.S_ISREG(lock_state.st_mode)
            or lock_state.st_nlink != 1
            or (path_state.st_dev, path_state.st_ino) != (lock_state.st_dev, lock_state.st_ino)
        ):
            raise SafetyError(
                f"Clawpatch release sweep lock is not a private regular file: {lock_path}"
            )
        if lock_state.st_size == 0:
            os.write(descriptor, b"\0")
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise SafetyError(
                    f"A Clawpatch release sweep is already active for this repository: {repo}"
                ) from exc
            raise SafetyError(
                f"Could not acquire the Clawpatch release sweep lock: {lock_path}: {exc}"
            ) from exc
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError as exc:
            raise SafetyError(
                f"Could not release the Clawpatch release sweep lock: {lock_path}: {exc}"
            ) from exc
        finally:
            os.close(descriptor)


_WINDOWS_CODEX_SANDBOX_MARKER = "CLAWPATCH_WINDOWS_CODEX_SANDBOX_OK"


def _clawpatch_doctor(repo: Path, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    result = CommandRunner().run(
        ["clawpatch", "doctor", "--json"],
        cwd=repo,
        timeout_seconds=60,
        env=env,
        kill_process_group=True,
    )
    if not result.passed:
        raise SafetyError(
            "ClawPatch doctor could not prove provider readiness.\n"
            f"stdout:\n{result.stdout[-4000:]}\n"
            f"stderr:\n{result.stderr[-4000:]}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SafetyError("ClawPatch doctor returned malformed JSON.") from exc
    if not isinstance(payload, dict) or payload.get("state") not in {"ok", "missing"}:
        raise SafetyError("ClawPatch doctor did not report a ready runtime.")
    provider = payload.get("provider")
    if not isinstance(provider, str) or not provider:
        raise SafetyError("ClawPatch doctor did not identify its provider.")
    return payload


def _windows_codex_sandbox_path(
    repo: Path,
    *,
    env: dict[str, str] | None = None,
    platform_name: str = os.name,
    required: bool = True,
) -> str | None:
    """Prefer the first Windows Codex launcher whose nested sandbox can execute."""
    if platform_name != "nt":
        return None
    process_env = dict(os.environ if env is None else env)
    original_path = process_env.get("PATH", "")
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw_directory in original_path.split(";"):
        directory_text = raw_directory.strip().strip('"')
        if not directory_text:
            continue
        directory = Path(directory_text)
        for name in ("codex.cmd", "codex.exe"):
            candidate = directory / name
            key = str(candidate).casefold()
            if key in seen or not candidate.is_file():
                continue
            seen.add(key)
            candidates.append(candidate)
    if not candidates:
        if not required:
            return None
        raise SafetyError(
            "ClawPatch selected the Codex provider, but no Windows Codex launcher is on PATH."
        )

    failures: list[str] = []
    for candidate in candidates:
        selected_path = str(candidate.parent) + (";" + original_path if original_path else "")
        candidate_env = dict(process_env)
        candidate_env["PATH"] = selected_path
        result = CommandRunner().run(
            [
                str(candidate),
                "sandbox",
                "cmd.exe",
                "/d",
                "/c",
                "echo",
                _WINDOWS_CODEX_SANDBOX_MARKER,
            ],
            cwd=repo,
            timeout_seconds=45,
            env=candidate_env,
            kill_process_group=True,
        )
        if result.passed and _WINDOWS_CODEX_SANDBOX_MARKER in result.stdout:
            return selected_path
        failures.append(str(candidate))

    if not required:
        return None
    raise SafetyError(
        "Every installed Codex launcher failed the Windows nested-sandbox check: "
        + ", ".join(failures)
        + ". Reinstall Codex under a short user-local path and remove broken duplicate launchers. "
        "No ClawPatch queue was started."
    )


def runtime_doctor(repo: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """Prove the portable runtime contract without creating or advancing a queue."""
    root = _git_root(repo)
    clawpatch_version = _clawpatch_version(root)
    env_overrides: dict[str, str] = {}
    selected_codex_path = _windows_codex_sandbox_path(root, required=False)
    if selected_codex_path is not None:
        env_overrides["PATH"] = selected_codex_path
    doctor_env = dict(os.environ)
    doctor_env.update(env_overrides)
    provider = _clawpatch_doctor(root, env=doctor_env)
    if (
        provider["provider"].casefold() == "codex"
        and os.name == "nt"
        and selected_codex_path is None
    ):
        selected_codex_path = _windows_codex_sandbox_path(root)
        if selected_codex_path is not None:
            env_overrides["PATH"] = selected_codex_path
    report = {
        "ready": True,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "git": _must_run(["git", "--version"], cwd=root, timeout=30).strip(),
        "clawpatch": clawpatch_version,
        "provider": provider["provider"],
        "providerVersion": provider.get("providerVersion"),
        "windowsCodexSandbox": "ready" if selected_codex_path is not None else "not-applicable",
    }
    return report, env_overrides


def require_external_clawpatch_preflight(repo: Path) -> dict[str, str]:
    """Prove tool, provider, Git, and process readiness before external service setup."""
    root = _git_root(repo)
    _report, env_overrides = runtime_doctor(root)
    _git_text(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _git_text(root, ["git", "rev-parse", "HEAD"])
    _require_no_process(root)
    return env_overrides


def _version_tuple(text: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise SafetyError(f"Could not read the installed Clawpatch version from: {text.strip()!r}")
    return tuple(int(value) for value in match.groups())


def _clawpatch_version(repo: Path) -> str:
    if not shutil.which("clawpatch"):
        raise SafetyError("Clawpatch is not installed or is not available on PATH.")
    text = _must_run(["clawpatch", "--version"], cwd=repo, timeout=30).strip()
    if _version_tuple(text) < MINIMUM_CLAWPATCH_VERSION:
        raise SafetyError("Clawpatch 0.7.2 or newer is required.")
    return text


def _run_clawpatch(
    repo: Path,
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    _require_no_process(repo)
    resolved_timeout = _child_timeout_seconds(env) if timeout is None else timeout
    return _run(
        argv,
        cwd=repo,
        timeout=resolved_timeout,
        env=env,
        kill_process_group=True,
    )


def _must_clawpatch(
    repo: Path,
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    phase: str | None = None,
    current: int | str = "?",
    total: int | str = "?",
    finding_id: str = "",
) -> str:
    resolved_timeout = _child_timeout_seconds(env) if timeout is None else timeout
    command_phase = phase or _clawpatch_command_phase(argv)
    if progress is not None:
        progress(
            {
                "phase": command_phase,
                "current": current,
                "total": total,
                "finding_id": finding_id,
                "command": shlex.join(argv),
                "attempt": 1,
                "max_attempts": 1,
            }
        )
    result = _run_clawpatch(repo, argv, env=env, timeout=resolved_timeout)
    if not result.returncode:
        return result.stdout
    output = result.stdout or ""
    if (
        result.returncode == 1
        and len(argv) >= 4
        and argv[1] == "show"
        and "finding not found:" in output.casefold()
    ):
        try:
            missing_id = argv[argv.index("--finding") + 1]
        except (ValueError, IndexError) as exc:
            raise SafetyError("Clawpatch show reported a missing finding without an ID.") from exc
        raise _MissingFinding(
            f"Clawpatch finding no longer exists: {missing_id}",
            finding_id=missing_id,
        )
    watchdog = (
        f"the {resolved_timeout}-second child watchdog expired"
        if result.returncode == 124
        else "command must exit 0"
    )
    raise _ClawpatchCommandFailure(
        f"phase: Clawpatch command\ncommand: {shlex.join(argv)}\nfinding ID: "
        f"{finding_id or 'N/A'}\nexit code: {result.returncode}\n"
        f"failed requirement: {watchdog}; this command is not retried\n"
        f"changed source paths: {_source_paths(repo)}\noutput:\n{output[-6000:]}",
        failure=classify_clawpatch_failure(command_phase, result.returncode),
    )


def _clawpatch_command_phase(argv: list[str]) -> str:
    command = argv[1] if len(argv) > 1 else "clawpatch"
    if command == "clean-locks":
        return "lock-cleanup"
    if command == "review" and "--dry-run" in argv:
        return "review-verification"
    if command == "next":
        return "queue"
    return command


def _json_clawpatch(
    repo: Path,
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    phase: str | None = None,
    current: int | str = "?",
    total: int | str = "?",
    finding_id: str = "",
) -> dict[str, Any]:
    output = _must_clawpatch(
        repo,
        argv,
        env=env,
        timeout=timeout,
        progress=progress,
        phase=phase,
        current=current,
        total=total,
        finding_id=finding_id,
    )
    return _parse_json_output(output, command=" ".join(argv[1:]))


def _next_finding(
    repo: Path,
    *,
    env: dict[str, str],
    status: str = "open",
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> tuple[str | None, dict[str, Any]]:
    if status not in {"open", "uncertain"}:
        raise SafetyError(f"Unsupported Clawpatch queue status {status!r}.")
    argv = ["clawpatch", "next", "--json"]
    if status != "open":
        argv = ["clawpatch", "next", "--status", status, "--json"]
    payload = _json_clawpatch(
        repo,
        argv,
        env=env,
        progress=progress,
        current=current,
        total=total,
    )
    finding = payload.get("finding")
    if finding is None:
        return None, payload
    if not isinstance(finding, dict):
        raise SafetyError("Clawpatch next returned a malformed finding value.")
    finding_id = finding.get("id")
    if not isinstance(finding_id, str) or not _FINDING_ID.fullmatch(finding_id):
        raise SafetyError("Clawpatch next returned no valid finding ID.")
    if finding.get("status") != status:
        raise SafetyError(
            f"Clawpatch next --status {status} returned a non-{status} finding {finding_id}."
        )
    expected_next = f"clawpatch show --finding {finding_id}"
    if payload.get("next") != expected_next:
        raise SafetyError(
            f"Clawpatch next returned an unexpected inspection command for {finding_id}."
        )
    return finding_id, payload


def _show_finding(
    repo: Path,
    finding_id: str,
    *,
    env: dict[str, str],
    required_status: str | None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> dict[str, Any]:
    payload = _json_clawpatch(
        repo,
        ["clawpatch", "show", "--finding", finding_id, "--json"],
        env=env,
        progress=progress,
        current=current,
        total=total,
        finding_id=finding_id,
    )
    finding = payload.get("finding")
    if not isinstance(finding, dict) or finding.get("id") != finding_id:
        raise SafetyError(f"Clawpatch show returned the wrong finding for {finding_id}.")
    if required_status is not None and finding.get("status") != required_status:
        raise SafetyError(
            f"Clawpatch show requires {finding_id} to have status {required_status!r}."
        )
    validation = payload.get("validation")
    if not isinstance(validation, list) or any(not isinstance(item, str) for item in validation):
        raise SafetyError(f"Clawpatch show returned malformed validation data for {finding_id}.")
    patch_attempts = payload.get("patchAttempts")
    if not isinstance(patch_attempts, list):
        raise SafetyError(f"Clawpatch show returned malformed patch attempts for {finding_id}.")
    return payload


def _finding_from_fix_argv(argv: list[str]) -> str:
    if len(argv) < 2 or argv[1] != "fix":
        raise SafetyError("Expected Clawpatch to direct a fix command.")
    try:
        value = argv[argv.index("--finding") + 1]
    except (ValueError, IndexError) as exc:
        raise SafetyError("Clawpatch fix command did not name a finding.") from exc
    if not _FINDING_ID.fullmatch(value):
        raise SafetyError(f"Clawpatch fix command returned an invalid finding ID: {value!r}")
    return value


def _with_json(argv: list[str]) -> list[str]:
    return list(argv) if "--json" in argv else [*argv, "--json"]


def _fix_command(
    repo: Path, argv: list[str], *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    finding_id = _finding_from_fix_argv(argv)
    command = _with_json(argv)
    result = _run_clawpatch(
        repo,
        command,
        env=env or dict(os.environ),
    )
    if result.returncode:
        requirement = (
            "clawpatch fix validation passed"
            if result.returncode == 6
            else "clawpatch fix exited 0"
        )
        message = (
            f"phase: fix\ncommand: {shlex.join(command)}\nfinding ID: {finding_id}\n"
            f"exit code: {result.returncode}\nfailed requirement: {requirement}\n"
            f"changed source paths: {_source_paths(repo) if repo.exists() else []}\n"
            f"output:\n{result.stdout[-6000:]}"
        )
        failure_outcomes = {
            1: "provider-failed",
            5: "provider-quota",
            6: "fix-validation-failed",
            124: "timeout",
        }
        failure = classify_clawpatch_failure("fix", result.returncode)
        if failure.progress_capable:
            raise _UnresolvedFinding(
                message,
                finding_id=finding_id,
                outcome=failure_outcomes.get(result.returncode, failure.kind.value),
                failure=failure,
            )
        raise SafetyError(message)
    payload = _parse_json_output(result.stdout, command="fix")
    if payload.get("finding") != finding_id:
        raise SafetyError(f"Clawpatch fix returned the wrong finding; expected {finding_id!r}.")
    if payload.get("status") != "applied":
        raise SafetyError(f"Clawpatch fix did not apply a validated patch for {finding_id}.")
    patch_attempt = payload.get("patchAttempt")
    if not isinstance(patch_attempt, str) or not patch_attempt.strip():
        raise SafetyError("Clawpatch fix returned no valid patch-attempt ID.")
    payload["patchAttempt"] = patch_attempt.strip()
    return payload


def _patch_attempt_from_show(
    show_payload: dict[str, Any], patch_attempt_id: str, finding_id: str
) -> dict[str, Any]:
    patch_attempts = show_payload.get("patchAttempts")
    if not isinstance(patch_attempts, list):
        raise SafetyError(f"Clawpatch show returned no patch-attempt list for {finding_id}.")
    value = next(
        (
            candidate
            for candidate in patch_attempts
            if isinstance(candidate, dict) and candidate.get("patchAttemptId") == patch_attempt_id
        ),
        None,
    )
    if not isinstance(value, dict):
        raise SafetyError(f"Could not read Clawpatch patch-attempt record {patch_attempt_id}.")
    finding_ids = value.get("findingIds")
    if not isinstance(finding_ids, list) or finding_id not in finding_ids:
        raise SafetyError(f"Clawpatch patch-attempt record does not belong to {finding_id}.")
    files = value.get("filesChanged")
    if not isinstance(files, list) or any(not isinstance(path, str) or not path for path in files):
        raise SafetyError("Clawpatch patch-attempt filesChanged is malformed.")
    return value


def _validate_attempt_paths(repo: Path, files: list[str]) -> None:
    _validate_attempt_paths_syntax(files)
    current = _source_paths(repo)
    if sorted(files) != current:
        raise SafetyError(
            "Changed source paths do not exactly match the current Clawpatch patch attempt; "
            f"attempt={sorted(files)!r}, current={current!r}."
        )


def _run_project_gates(
    repo: Path,
    *,
    finding_id: str,
    required: bool = True,
) -> list[dict[str, Any]]:
    config_path = repo / PROJECT_DIR / "config.toml"
    if not config_path.is_file():
        if not required:
            return []
        raise SafetyError("The repository has no configured validation gates.")
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SafetyError(f"Could not read validation configuration: {config_path}") from exc
    allowed = config.get("safety", {}).get("allowed_programs", [])
    gates = config.get("verification", {}).get("gates", [])
    if (
        not isinstance(allowed, list)
        or any(not isinstance(item, str) or not item for item in allowed)
        or not isinstance(gates, list)
        or not gates
    ):
        raise SafetyError("The repository has no valid configured validation gates.")
    log_root = repo / PROJECT_DIR / "cache" / "clawpatch-release-logs"
    runner = CommandRunner(log_root=log_root)
    outcomes = []
    failures = []
    for item in gates:
        if not isinstance(item, dict):
            raise SafetyError("Validation gate configuration is malformed.")
        gate_id = item.get("id")
        argv = item.get("argv")
        timeout = item.get("timeout_seconds", 1800)
        required_gate = item.get("required", True)
        if (
            not isinstance(gate_id, str)
            or not gate_id
            or not isinstance(argv, list)
            or not argv
            or any(not isinstance(argument, str) or not argument for argument in argv)
            or isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or timeout <= 0
            or not isinstance(required_gate, bool)
        ):
            raise SafetyError("Validation gate configuration is malformed.")
        executable = Path(argv[0]).name
        if executable not in allowed:
            raise SafetyError(
                f"Validation gate {gate_id!r} uses unapproved executable {executable!r}."
            )
        result = runner.run(
            argv,
            cwd=repo,
            timeout_seconds=timeout,
            log_name=f"gate-{gate_id}",
        )
        outcome = {
            "gate": {
                "id": gate_id,
                "kind": str(item.get("kind", "check")),
                "argv": list(argv),
                "required": required_gate,
                "timeout_seconds": timeout,
            },
            "result": result.to_dict(),
        }
        outcomes.append(outcome)
        if required_gate and not result.passed:
            failures.append(outcome)
    if failures:
        details = []
        for failure in failures:
            gate = failure["gate"]
            result = failure["result"]
            output = "\n".join(
                value
                for value in (result.get("stdout"), result.get("stderr"))
                if isinstance(value, str) and value
            )
            details.append(
                f"gate: {gate['id']}\ncommand: {shlex.join(gate['argv'])}\n"
                f"exit code: {result.get('exit_code')}\noutput:\n{output[-6000:]}"
            )
        raise GateFailure(
            f"phase: project validation\ncommand: configured gates\nfinding ID: {finding_id}\n"
            "exit code: nonzero\nfailed requirement: complete repository validation must pass\n"
            f"changed source paths: {_source_paths(repo)}\noutput:\n" + "\n\n".join(details)
        )
    return outcomes


def _source_state_fingerprint_for_paths(repo: Path, paths: list[str]) -> dict[str, Any]:
    diff = (
        _must_run(
            ["git", "diff", "--binary", "--full-index", "--no-ext-diff", "HEAD", "--", *paths],
            cwd=repo,
            timeout=120,
        )
        if paths
        else ""
    )
    untracked = (
        sorted(
            path
            for path in _must_run(
                ["git", "ls-files", "--others", "--exclude-standard", "-z", "--", *paths],
                cwd=repo,
                timeout=120,
            ).split("\0")
            if path
        )
        if paths
        else []
    )
    untracked_hashes = {path: _untracked_path_fingerprint(repo, path) for path in untracked}
    dirty_gitlinks = sorted(set(paths).intersection(_gitlink_paths(repo)))
    gitlinks: dict[str, Any] = {}
    for path in dirty_gitlinks:
        nested = repo / path
        if not nested.is_dir():
            raise SafetyError(f"Dirty Git submodule path is unavailable: {path}")
        gitlinks[path] = {
            "head": _git_text(nested, ["git", "rev-parse", "HEAD"]),
            **_source_state_fingerprint(nested),
        }
    return {
        "paths": paths,
        "diff": diff,
        "untracked": untracked_hashes,
        "gitlinks": gitlinks,
    }


def _source_state_fingerprint(repo: Path) -> dict[str, Any]:
    return _source_state_fingerprint_for_paths(repo, _source_paths(repo))


def _untracked_path_fingerprint(repo: Path, path: str) -> str:
    candidate = repo / path
    if candidate.is_symlink():
        target = os.readlink(candidate)
        digest = hashlib.sha256(os.fsencode(target)).hexdigest()
        return f"symlink:{digest}"
    return _git_text(repo, ["git", "hash-object", "--no-filters", "--", path])


def _owned_source_fingerprint(repo: Path, paths: list[str]) -> str:
    exact_paths = sorted(set(paths))
    if not exact_paths or _source_paths(repo) != exact_paths:
        return ""
    return _source_paths_fingerprint(repo, exact_paths)


def _source_paths_fingerprint(repo: Path, paths: list[str]) -> str:
    exact_paths = sorted(set(paths))
    if not exact_paths:
        return ""
    payload = json.dumps(
        _source_state_fingerprint_for_paths(repo, exact_paths),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _legacy_owned_source_fingerprint(repo: Path, paths: list[str]) -> str:
    exact_paths = sorted(set(paths))
    if not exact_paths or _source_paths(repo) != exact_paths:
        return ""
    state = _source_state_fingerprint(repo)
    # Legacy proofs did not safely bind nested Git content or untracked symlinks.
    if state["gitlinks"] or any((repo / path).is_symlink() for path in state["untracked"]):
        return ""
    state.pop("gitlinks")
    payload = json.dumps(
        state,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _revalidation_payload(
    repo: Path,
    finding_id: str,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    phase: str = "revalidate",
    current: int | str = "?",
    total: int | str = "?",
) -> tuple[list[str], dict[str, Any], str]:
    argv = ["clawpatch", "revalidate", "--finding", finding_id, "--json"]
    payload = _json_clawpatch(
        repo,
        argv,
        env=env,
        progress=progress,
        phase=phase,
        current=current,
        total=total,
        finding_id=finding_id,
    )
    outcome = payload.get("outcome")
    if payload.get("finding") != finding_id or outcome not in {
        "fixed",
        "open",
        "uncertain",
        "false-positive",
    }:
        raise SafetyError(
            f"phase: revalidation\ncommand: {shlex.join(argv)}\nfinding ID: {finding_id}\n"
            "exit code: 0\nfailed requirement: matching finding and a documented outcome\n"
            f"changed source paths: {_source_paths(repo)}\noutput:\n{json.dumps(payload, sort_keys=True)}"
        )
    return argv, payload, str(outcome)


def _revalidate(
    repo: Path,
    finding_id: str,
    *,
    env: dict[str, str],
    expected_paths: list[str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> dict[str, Any]:
    if sorted(expected_paths) != _source_paths(repo):
        raise SafetyError(
            "Revalidation source paths no longer match the validated Clawpatch patch attempt."
        )
    before = _source_state_fingerprint(repo)
    argv = ["clawpatch", "revalidate", "--finding", finding_id, "--json"]
    try:
        argv, payload, outcome = _revalidation_payload(
            repo,
            finding_id,
            env=env,
            progress=progress,
            current=current,
            total=total,
        )
    except SafetyError as exc:
        after = _source_state_fingerprint(repo)
        if isinstance(exc, _ClawpatchCommandFailure):
            failure_outcome = (
                "revalidation-provider-failed"
                if exc.failure.kind.value == "provider-refused"
                else f"revalidation-{exc.failure.kind.value}"
            )
            raise _UnresolvedFinding(
                str(exc),
                finding_id=finding_id,
                outcome=failure_outcome,
                failure=exc.failure,
            ) from exc
        if after != before:
            raise _UnresolvedFinding(
                f"{exc}\nfailed requirement: failed revalidation source progress must be "
                "preserved and retried on the same finding",
                finding_id=finding_id,
                outcome="revalidation-command-failed-with-source-progress",
                failure=classify_clawpatch_failure("revalidation", 23),
            ) from exc
        raise
    if outcome in {"open", "uncertain"} and env.get("CLAWPATCH_CODEX_SANDBOX") in {
        None,
        "read-only",
    }:
        initial_outcome = outcome
        escalated_env = dict(env)
        escalated_env["CLAWPATCH_CODEX_SANDBOX"] = "workspace-write"
        _argv, escalated, escalated_outcome = _revalidation_payload(
            repo,
            finding_id,
            env=escalated_env,
            progress=progress,
            phase="revalidate-escalated",
            current=current,
            total=total,
        )
        payload = dict(escalated)
        payload["managerooSandboxEscalated"] = True
        payload["managerooInitialOutcome"] = initial_outcome
        outcome = escalated_outcome
        if (
            outcome in {"open", "uncertain"}
            and env.get("MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK") == "1"
        ):
            workspace_write_outcome = outcome
            host_env = dict(env)
            host_env["CLAWPATCH_CODEX_SANDBOX"] = "bypass"
            _argv, host_payload, host_outcome = _revalidation_payload(
                repo,
                finding_id,
                env=host_env,
                progress=progress,
                phase="revalidate-host",
                current=current,
                total=total,
            )
            payload = dict(host_payload)
            payload["managerooSandboxEscalated"] = True
            payload["managerooHostSandboxBypassed"] = True
            payload["managerooInitialOutcome"] = initial_outcome
            payload["managerooWorkspaceWriteOutcome"] = workspace_write_outcome
            outcome = host_outcome
    after = _source_state_fingerprint(repo)
    if after != before:
        raise _UnresolvedFinding(
            f"phase: revalidation\ncommand: {shlex.join(argv)}\nfinding ID: {finding_id}\n"
            "exit code: 0\nfailed requirement: revalidation must not alter source\n"
            f"changed source paths: {_source_paths(repo)}",
            finding_id=finding_id,
            outcome="revalidation-mutated-source",
            failure=classify_clawpatch_failure("revalidation", 23),
        )
    if outcome not in {"fixed", "open", "uncertain", "false-positive"}:
        raise _UnresolvedFinding(
            f"phase: revalidation\ncommand: {shlex.join(argv)}\nfinding ID: {finding_id}\n"
            "exit code: 0\nfailed requirement: exact lowercase outcome fixed, open, "
            "uncertain, or "
            f"false-positive; received {outcome}\n"
            f"changed source paths: {_source_paths(repo)}\n"
            f"output:\n{json.dumps(payload, sort_keys=True)}",
            finding_id=finding_id,
            outcome=str(outcome),
        )
    return payload


def _external_state_home() -> Path:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "ClawPatchSupervise" / "state"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "clawpatch-supervise"


def _legacy_external_state_homes() -> tuple[Path, ...]:
    homes: list[Path] = []
    prefix = Path(sys.prefix).resolve()
    base_prefix = Path(sys.base_prefix).resolve()
    if prefix != base_prefix and prefix.name.casefold() in {"venv", ".venv"}:
        homes.append(prefix.parent / "state")
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        homes.append(base / "Manageroo" / "clawpatch-supervise" / "state")
    else:
        xdg_state_home = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
        homes.append(base / "manageroo" / "clawpatch-supervise")
        homes.append(Path.home() / ".local" / "share" / "clawpatch-supervise" / "state")
    canonical = _external_state_home().resolve()
    return tuple(dict.fromkeys(home.resolve() for home in homes if home.resolve() != canonical))


def _repository_state_root(home: Path, repo: Path) -> Path:
    identity = hashlib.sha256(os.fsencode(str(repo.resolve()))).hexdigest()
    return home / "repositories" / identity


def _release_state_root(repo: Path, *, integration_mode: str) -> Path:
    if integration_mode == "manageroo":
        return repo / PROJECT_DIR / "cache"
    if integration_mode == "external":
        home = _external_state_home().resolve()
        repositories = home / "repositories"
        path = _repository_state_root(home, repo)
        for candidate in (home, repositories, path):
            if candidate.is_symlink():
                raise SafetyError(f"External Clawpatch state path cannot be a symlink: {candidate}")
        return path
    raise SafetyError("integration_mode must be one of: manageroo, external.")


def external_state_root(repo: Path) -> Path:
    """Return the durable standalone state directory for a repository."""
    return _release_state_root(repo.resolve(), integration_mode="external")


def _release_progress_path(repo: Path, *, state_root: Path | None = None) -> Path:
    root = state_root if state_root is not None else repo / PROJECT_DIR / "cache"
    return root / "clawpatch-release-progress.json"


def _write_release_progress(
    repo: Path,
    *,
    finding_id: str,
    branch: str,
    head_before: str,
    phase: str,
    owned_paths: list[str] | None = None,
    temporary_commit: str = "",
    source_states: list[str] | None = None,
    last_action: RepairAction | str | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    if not _FINDING_ID.fullmatch(finding_id):
        raise SafetyError(f"Cannot checkpoint invalid Clawpatch finding ID {finding_id!r}.")
    exact_owned_paths = sorted(set(owned_paths or []))
    _validate_attempt_paths_syntax(exact_owned_paths)
    if (
        not branch
        or not head_before
        or not phase
        or (temporary_commit and not re.fullmatch(r"[0-9a-f]{40,64}", temporary_commit))
        or any(
            not isinstance(state, str) or not re.fullmatch(r"[0-9a-f]{40,64}", state)
            for state in (source_states or [])
        )
    ):
        raise SafetyError("Cannot checkpoint malformed Clawpatch release progress.")
    action_value = (
        last_action.value if isinstance(last_action, RepairAction) else (last_action or "")
    )
    if action_value and action_value not in {action.value for action in RepairAction}:
        raise SafetyError("Cannot checkpoint an unknown Clawpatch repair action.")
    progress = {
        "version": RELEASE_PROGRESS_VERSION,
        "repo": str(repo.resolve()),
        "finding_id": finding_id,
        "branch": branch,
        "head_before": head_before,
        "owned_paths": exact_owned_paths,
        "owned_source_fingerprint": _owned_source_fingerprint(repo, exact_owned_paths),
        "temporary_commit": temporary_commit,
        "source_states": list(dict.fromkeys(source_states or [])),
        "phase": phase,
        "last_action": action_value,
        "updated_at": utc_now(),
    }
    atomic_write_json(_release_progress_path(repo, state_root=state_root), progress)
    return progress


def _load_release_progress(
    repo: Path,
    *,
    state_root: Path | None = None,
) -> dict[str, Any] | None:
    path = _release_progress_path(repo, state_root=state_root)
    if not path.is_file():
        return None
    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyError(f"Clawpatch release progress is unreadable: {path}") from exc
    if not isinstance(progress, dict):
        raise SafetyError("Clawpatch release progress is malformed.")
    progress = dict(progress)
    required_strings = ("repo", "finding_id", "branch", "head_before", "phase", "updated_at")
    stored_version = progress.get("version")
    if (
        stored_version not in {2, 3, 4, 5, RELEASE_PROGRESS_VERSION}
        or any(
            not isinstance(progress.get(field), str) or not progress[field]
            for field in required_strings
        )
        or not _FINDING_ID.fullmatch(str(progress.get("finding_id", "")))
        or not isinstance(progress.get("owned_paths"), list)
        or any(not isinstance(path, str) or not path for path in progress.get("owned_paths", []))
    ):
        raise SafetyError("Clawpatch release progress is malformed.")
    owned_source_fingerprint = progress.get("owned_source_fingerprint", "")
    if not isinstance(owned_source_fingerprint, str) or (
        owned_source_fingerprint and not re.fullmatch(r"[0-9a-f]{64}", owned_source_fingerprint)
    ):
        raise SafetyError("Clawpatch release progress has a malformed source fingerprint.")
    progress["owned_source_fingerprint"] = owned_source_fingerprint
    temporary_commit = progress.get("temporary_commit", "")
    source_states = progress.get("source_states", [])
    if (
        not isinstance(temporary_commit, str)
        or (temporary_commit and not re.fullmatch(r"[0-9a-f]{40,64}", temporary_commit))
        or not isinstance(source_states, list)
        or any(
            not isinstance(state, str) or not re.fullmatch(r"[0-9a-f]{40,64}", state)
            for state in source_states
        )
    ):
        raise SafetyError("Clawpatch release progress has malformed iteration ownership.")
    progress["temporary_commit"] = temporary_commit
    progress["source_states"] = list(dict.fromkeys(source_states))
    last_action = progress.get("last_action", "")
    if not isinstance(last_action, str) or (
        last_action and last_action not in {action.value for action in RepairAction}
    ):
        raise SafetyError("Clawpatch release progress has an unknown repair action.")
    progress["last_action"] = last_action
    _validate_attempt_paths_syntax(list(progress["owned_paths"]))
    if Path(progress["repo"]).resolve() != repo.resolve():
        raise SafetyError("Clawpatch release progress belongs to a different repository.")
    return progress


def _migrate_legacy_external_progress(repo: Path, *, state_root: Path) -> None:
    legacy_roots = [repo / PROJECT_DIR / "cache"]
    legacy_roots.extend(
        _repository_state_root(home, repo) for home in _legacy_external_state_homes()
    )

    def upgrade_fingerprint(progress: dict[str, Any]) -> dict[str, Any]:
        if progress.get("version") not in {4, 5}:
            return progress
        owned_paths = list(progress["owned_paths"])
        recorded = str(progress.get("owned_source_fingerprint", ""))
        current = _owned_source_fingerprint(repo, owned_paths)
        if not recorded or not current or recorded == current:
            return progress
        if recorded != _legacy_owned_source_fingerprint(repo, owned_paths):
            return progress
        upgraded = dict(progress)
        upgraded["version"] = RELEASE_PROGRESS_VERSION
        upgraded["owned_source_fingerprint"] = current
        return upgraded

    legacy_records: list[tuple[Path, dict[str, Any]]] = []
    for legacy_root in dict.fromkeys(legacy_roots):
        legacy_path = _release_progress_path(repo, state_root=legacy_root)
        if not legacy_path.is_file():
            continue
        legacy = _load_release_progress(repo, state_root=legacy_root)
        if legacy is not None:
            legacy_records.append((legacy_root, upgrade_fingerprint(legacy)))
    current_path = _release_progress_path(repo, state_root=state_root)
    current = (
        _load_release_progress(repo, state_root=state_root) if current_path.is_file() else None
    )
    upgraded_current = upgrade_fingerprint(current) if current is not None else None
    if current is not None and upgraded_current != current:
        atomic_write_json(current_path, upgraded_current)
        if _load_release_progress(repo, state_root=state_root) != upgraded_current:
            raise SafetyError("External Clawpatch fingerprint migration could not be verified.")
    if not legacy_records:
        return
    legacy = legacy_records[0][1]
    if any(record != legacy for _root, record in legacy_records[1:]):
        raise SafetyError(
            "External Clawpatch progress exists in multiple legacy state locations "
            "with different ownership records."
        )
    if current_path.is_file():
        current = _load_release_progress(repo, state_root=state_root)
        if current != legacy:
            raise SafetyError(
                "External Clawpatch progress exists in both legacy and current state "
                "locations with different ownership records."
            )
    else:
        atomic_write_json(current_path, legacy)
        if _load_release_progress(repo, state_root=state_root) != legacy:
            raise SafetyError("External Clawpatch progress migration could not be verified.")
    for legacy_root, _record in legacy_records:
        _release_progress_path(repo, state_root=legacy_root).unlink()
        for directory in (legacy_root, repo / PROJECT_DIR):
            try:
                directory.rmdir()
            except OSError:
                pass


def _checkpoint_can_follow_supervisor_upgrade(
    repo: Path,
    progress: dict[str, Any],
) -> bool:
    if progress.get("phase") not in {
        "fix",
        "stopped",
    }:
        return False
    finding_id = progress.get("finding_id")
    old_head = progress.get("head_before")
    if not isinstance(finding_id, str) or not isinstance(old_head, str):
        return False
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    ancestor = _run(
        ["git", "merge-base", "--is-ancestor", old_head, current_head],
        cwd=repo,
        timeout=60,
    )
    if ancestor.returncode:
        return False
    changed_output = _must_run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", f"{old_head}..{current_head}"],
        cwd=repo,
        timeout=60,
    )
    changed_paths = {line.strip() for line in changed_output.splitlines() if line.strip()}
    if not changed_paths or not changed_paths.issubset(_SUPERVISOR_UPGRADE_PATHS):
        return False
    finding_path = repo / ".clawpatch" / "findings" / f"{finding_id}.json"
    try:
        finding = json.loads(finding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    evidence = finding.get("evidence") if isinstance(finding, dict) else None
    if not isinstance(evidence, list):
        return False
    evidence_paths = {
        item["path"]
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    return changed_paths.isdisjoint(evidence_paths)


def _checkpoint_completed_commit(
    repo: Path,
    progress: dict[str, Any],
) -> str:
    if progress.get("phase") != "stopped":
        return ""
    old_head = progress.get("head_before")
    owned_paths = progress.get("owned_paths")
    if not isinstance(old_head, str) or not isinstance(owned_paths, list) or not owned_paths:
        return ""
    expected = sorted(str(path) for path in owned_paths)
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    ancestor = _run(
        ["git", "merge-base", "--is-ancestor", old_head, current_head],
        cwd=repo,
        timeout=60,
    )
    if ancestor.returncode:
        return ""
    commits = _must_run(
        ["git", "rev-list", "--reverse", f"{old_head}..{current_head}"],
        cwd=repo,
        timeout=60,
    ).splitlines()
    for commit in commits:
        changed = _must_run(
            [
                "git",
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "--no-renames",
                "-r",
                commit,
            ],
            cwd=repo,
            timeout=60,
        ).splitlines()
        source_paths = sorted(
            path
            for path in changed
            if path and path != ".clawpatch" and not path.startswith(".clawpatch/")
        )
        if source_paths == expected:
            return commit
    return ""


def _clean_descendant_retires_verified_checkpoint(
    repo: Path,
    progress: dict[str, Any],
) -> bool:
    """Retire only the recovery wrapper after its clean base has safely advanced.

    The ClawPatch finding remains in ``.clawpatch`` and is selected normally on
    the continuing run. This does not classify, skip, or otherwise advance it.
    """
    if progress.get("phase") != "stopped" or _source_paths(repo):
        return False
    finding_id = progress.get("finding_id")
    original_head = progress.get("head_before")
    temporary_commit = progress.get("temporary_commit")
    owned_paths = progress.get("owned_paths")
    if (
        not isinstance(finding_id, str)
        or not isinstance(original_head, str)
        or not isinstance(temporary_commit, str)
        or not isinstance(owned_paths, list)
        or bool(temporary_commit) != bool(owned_paths)
    ):
        return False
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    if current_head == original_head:
        return False
    ancestor = _run(
        ["git", "merge-base", "--is-ancestor", original_head, current_head],
        cwd=repo,
        timeout=60,
    )
    if ancestor.returncode:
        return False
    if owned_paths:
        try:
            iteration_paths = _verify_iteration_commit(
                repo,
                finding_id=finding_id,
                original_head=original_head,
                temporary_commit=temporary_commit,
                require_current=False,
            )
        except SafetyError:
            return False
        if not iteration_paths or not set(iteration_paths).issubset(set(owned_paths)):
            return False
    finding_path = repo / ".clawpatch" / "findings" / f"{finding_id}.json"
    if finding_path.is_symlink() or not finding_path.is_file():
        return False
    try:
        finding = json.loads(finding_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(finding, dict) and finding.get("findingId") == finding_id


def _checkpoint_unapplied_attempt(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if (
        progress_record.get("phase") != "stopped"
        or progress_record.get("owned_paths") != []
        or _source_paths(repo)
    ):
        return None
    finding_id = str(progress_record["finding_id"])
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    if progress_record.get("head_before") != current_head:
        return None
    if inspected is None:
        inspected = _show_finding(
            repo,
            finding_id,
            env=env,
            required_status=None,
            progress=progress,
            current=1,
            total="?",
        )
    if inspected["finding"].get("status") != "open":
        return None
    resumable_statuses = {"planned"}
    if progress_record.get("last_action") == RepairAction.STOP_TRANSIENT.value:
        resumable_statuses.add("failed")
    planned_attempts = []
    for attempt in inspected["patchAttempts"]:
        if not isinstance(attempt, dict) or attempt.get("status") not in resumable_statuses:
            continue
        git_record = attempt.get("git")
        finding_ids = attempt.get("findingIds")
        if (
            isinstance(finding_ids, list)
            and finding_id in finding_ids
            and attempt.get("filesChanged") == []
            and isinstance(git_record, dict)
            and git_record.get("baseSha") == current_head
            and isinstance(attempt.get("patchAttemptId"), str)
            and attempt["patchAttemptId"]
        ):
            planned_attempts.append(str(attempt["patchAttemptId"]))
    if not planned_attempts:
        return None
    return {
        "finding_id": finding_id,
        "patch_attempts": planned_attempts,
        "inspection": inspected,
    }


def _recover_interrupted_source_clean_fix(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    state_root: Path,
) -> dict[str, Any] | None:
    """Turn one provably interrupted pre-edit fix checkpoint into a resumable stop."""
    if (
        progress_record.get("phase") != "fix"
        or progress_record.get("owned_paths") != []
        or progress_record.get("temporary_commit")
        or progress_record.get("last_action")
        or _source_paths(repo)
    ):
        return None
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    checkpoint_head = str(progress_record["head_before"])
    if checkpoint_head != current_head:
        ancestry = _run(
            ["git", "merge-base", "--is-ancestor", checkpoint_head, current_head],
            cwd=repo,
            timeout=60,
        )
        if ancestry.returncode != 0:
            return None
    checkpoint_tree = _git_text(repo, ["git", "rev-parse", f"{checkpoint_head}^{{tree}}"])
    current_tree = _git_text(repo, ["git", "rev-parse", "HEAD^{tree}"])
    source_states = progress_record.get("source_states")
    if (
        not isinstance(source_states, list)
        or not source_states
        or set(source_states) != {checkpoint_tree}
    ):
        return None
    return _write_release_progress(
        repo,
        finding_id=str(progress_record["finding_id"]),
        branch=str(progress_record["branch"]),
        head_before=current_head,
        phase="stopped",
        owned_paths=[],
        source_states=[current_tree],
        last_action=RepairAction.STOP_TRANSIENT,
        state_root=state_root,
    )


def _attempt_base_preserves_owned_source(
    repo: Path,
    *,
    attempt_base: Any,
    current_head: str,
    owned_paths: list[str],
) -> bool:
    if attempt_base == current_head:
        return True
    if not isinstance(attempt_base, str) or not re.fullmatch(r"[0-9a-f]{40,64}", attempt_base):
        return False
    ancestry = _run(
        ["git", "merge-base", "--is-ancestor", attempt_base, current_head],
        cwd=repo,
        timeout=60,
    )
    if ancestry.returncode != 0:
        return False
    unchanged = _run(
        ["git", "diff", "--quiet", attempt_base, current_head, "--", *owned_paths],
        cwd=repo,
        timeout=60,
    )
    return unchanged.returncode == 0


def _checkpoint_later_applied_attempt(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    inspected: dict[str, Any],
) -> dict[str, Any] | None:
    """Recognize one exact later ClawPatch repair after an interrupted checkpoint."""
    if progress_record.get("phase") != "stopped":
        return None
    finding_id = str(progress_record["finding_id"])
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    source_paths = _source_paths(repo)
    if not source_paths:
        return None
    checkpoint_head = str(progress_record.get("head_before", ""))
    checkpoint_paths = sorted(str(path) for path in progress_record.get("owned_paths", []))
    if checkpoint_head != current_head:
        ancestry = _run(
            ["git", "merge-base", "--is-ancestor", checkpoint_head, current_head],
            cwd=repo,
            timeout=60,
        )
        if ancestry.returncode != 0 or checkpoint_paths != source_paths:
            return None
        temporary_commit = str(progress_record.get("temporary_commit", ""))
        if not temporary_commit:
            return None
        try:
            iteration_paths = _verify_iteration_commit(
                repo,
                finding_id=finding_id,
                original_head=checkpoint_head,
                temporary_commit=temporary_commit,
                require_current=False,
            )
        except SafetyError:
            return None
        if not iteration_paths or not set(iteration_paths).issubset(checkpoint_paths):
            return None
    elif checkpoint_paths and checkpoint_paths != source_paths:
        return None
    if inspected["finding"].get("status") not in {
        "open",
        "uncertain",
        "fixed",
        "false-positive",
    }:
        return None
    matching_attempts = []
    for attempt in inspected["patchAttempts"]:
        if not isinstance(attempt, dict) or attempt.get("status") not in {"applied", "failed"}:
            continue
        finding_ids = attempt.get("findingIds")
        files_changed = attempt.get("filesChanged")
        git_record = attempt.get("git")
        patch_attempt_id = attempt.get("patchAttemptId")
        if (
            isinstance(finding_ids, list)
            and finding_id in finding_ids
            and isinstance(files_changed, list)
            and sorted(files_changed) == source_paths
            and isinstance(git_record, dict)
            and _attempt_base_preserves_owned_source(
                repo,
                attempt_base=git_record.get("baseSha"),
                current_head=current_head,
                owned_paths=source_paths,
            )
            and isinstance(patch_attempt_id, str)
            and patch_attempt_id
        ):
            _validate_attempt_paths_syntax(files_changed)
            if attempt.get("status") == "failed":
                attempt_time = _parse_checkpoint_time(attempt.get("updatedAt"))
                checkpoint_time = _parse_checkpoint_time(progress_record.get("updated_at"))
                if (
                    inspected["finding"].get("status") not in {"open", "uncertain"}
                    or attempt_time is None
                    or checkpoint_time is None
                    or attempt_time <= checkpoint_time
                ):
                    continue
                try:
                    if any(
                        (repo / path).is_symlink()
                        or not (repo / path).is_file()
                        or datetime.fromtimestamp((repo / path).stat().st_mtime, timezone.utc)
                        > attempt_time
                        for path in source_paths
                    ):
                        continue
                except OSError:
                    continue
            matching_attempts.append(attempt)
    if not matching_attempts:
        return None
    if (
        any(attempt.get("status") == "failed" for attempt in matching_attempts)
        and len(matching_attempts) != 1
    ):
        return None
    _validate_attempt_paths(repo, source_paths)
    return {
        "finding_id": finding_id,
        "patch_attempt": matching_attempts[-1],
        "patch_attempts": matching_attempts,
        "inspection": inspected,
        "owned_paths": source_paths,
    }


def _checkpoint_fixed_without_source(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if (
        progress_record.get("phase") != "stopped"
        or progress_record.get("owned_paths") != []
        or _source_paths(repo)
    ):
        return None
    finding_id = str(progress_record["finding_id"])
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    if progress_record.get("head_before") != current_head:
        return None
    if inspected is None:
        inspected = _show_finding(
            repo,
            finding_id,
            env=env,
            required_status=None,
            progress=progress,
            current=1,
            total="?",
        )
    if inspected["finding"].get("status") != "fixed":
        return None
    applied_attempts = []
    for attempt in inspected["patchAttempts"]:
        if not isinstance(attempt, dict) or attempt.get("status") != "applied":
            continue
        git_record = attempt.get("git")
        finding_ids = attempt.get("findingIds")
        if (
            isinstance(finding_ids, list)
            and finding_id in finding_ids
            and attempt.get("filesChanged") == []
            and isinstance(git_record, dict)
            and git_record.get("baseSha") == current_head
            and isinstance(attempt.get("patchAttemptId"), str)
            and attempt["patchAttemptId"]
        ):
            applied_attempts.append(str(attempt["patchAttemptId"]))
    if not applied_attempts:
        raise SafetyError(
            "A fixed source-clean checkpoint requires an applied zero-file patch attempt "
            "bound to the same finding and current HEAD."
        )
    return {
        "finding_id": finding_id,
        "patch_attempt": applied_attempts[-1],
        "patch_attempts": applied_attempts,
        "inspection": inspected,
        "head_before": current_head,
    }


def _checkpoint_false_positive_without_source(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if (
        progress_record.get("phase") != "stopped"
        or progress_record.get("owned_paths") != []
        or progress_record.get("last_action")
        not in {
            RepairAction.STOP_TERMINAL.value,
            RepairAction.DISCARD_AND_CONTINUE.value,
        }
        or _source_paths(repo)
    ):
        return None
    finding_id = str(progress_record["finding_id"])
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    temporary_commit = str(progress_record.get("temporary_commit", ""))
    if progress_record.get("head_before") != current_head or not temporary_commit:
        return None
    if inspected is None:
        inspected = _show_finding(
            repo,
            finding_id,
            env=env,
            required_status=None,
            progress=progress,
            current=1,
            total="?",
        )
    if inspected["finding"].get("status") != "false-positive":
        return None
    temporary_paths = _verify_iteration_commit(
        repo,
        finding_id=finding_id,
        original_head=current_head,
        temporary_commit=temporary_commit,
        require_current=False,
    )
    if not temporary_paths:
        raise SafetyError(
            "A false-positive source-clean checkpoint requires a nonempty exact temporary "
            "iteration commit."
        )
    matching_attempts = []
    for attempt in inspected["patchAttempts"]:
        if not isinstance(attempt, dict) or attempt.get("status") != "applied":
            continue
        git_record = attempt.get("git")
        finding_ids = attempt.get("findingIds")
        files_changed = attempt.get("filesChanged")
        if (
            isinstance(finding_ids, list)
            and finding_id in finding_ids
            and isinstance(files_changed, list)
            and sorted(files_changed) == temporary_paths
            and isinstance(git_record, dict)
            and git_record.get("baseSha") in {current_head, temporary_commit}
            and isinstance(attempt.get("patchAttemptId"), str)
            and attempt["patchAttemptId"]
        ):
            matching_attempts.append(str(attempt["patchAttemptId"]))
    if not matching_attempts:
        raise SafetyError(
            "A false-positive source-clean checkpoint requires an applied patch attempt bound "
            "to the same finding, exact temporary paths, and Git boundary."
        )
    return {
        "finding_id": finding_id,
        "patch_attempts": matching_attempts,
        "inspection": inspected,
        "head_before": current_head,
        "temporary_commit": temporary_commit,
        "discarded_paths": temporary_paths,
    }


def _clear_release_progress(repo: Path, *, state_root: Path | None = None) -> None:
    _release_progress_path(repo, state_root=state_root).unlink(missing_ok=True)


def _parse_checkpoint_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _empty_clawpatch_history(repo: Path) -> bool:
    state = repo / ".clawpatch"
    if state.is_symlink() or not state.is_dir():
        return False
    for name in ("findings", "patches", "runs", "reports"):
        directory = state / name
        if directory.exists() and (
            directory.is_symlink()
            or not directory.is_dir()
            or any(path.is_file() or path.is_symlink() for path in directory.rglob("*"))
        ):
            return False
    return True


def _rebuilt_generation_owns_checkpoint_source(
    repo: Path,
    progress_record: dict[str, Any],
) -> bool:
    """Prove a manual .clawpatch reset superseded one exact stopped attempt."""
    owned_paths = sorted(str(path) for path in progress_record.get("owned_paths", []))
    if (
        progress_record.get("phase") != "stopped"
        or not owned_paths
        or _source_paths(repo) != owned_paths
        or not _empty_clawpatch_history(repo)
    ):
        return False
    finding_id = str(progress_record.get("finding_id", ""))
    if (repo / ".clawpatch" / "findings" / f"{finding_id}.json").exists():
        return False
    project_path = repo / ".clawpatch" / "project.json"
    if project_path.is_symlink() or not project_path.is_file():
        return False
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(project, dict) or not isinstance(project.get("git"), dict):
        return False
    project_git = project["git"]
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    current_branch = _git_text(repo, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if (
        project_git.get("headSha") != current_head
        or project_git.get("currentBranch") != current_branch
        or progress_record.get("head_before") != current_head
        or progress_record.get("branch") != current_branch
    ):
        return False
    generation_time = _parse_checkpoint_time(project.get("createdAt"))
    checkpoint_time = _parse_checkpoint_time(progress_record.get("updated_at"))
    if generation_time is None or checkpoint_time is None or generation_time <= checkpoint_time:
        return False
    recorded_fingerprint = str(progress_record.get("owned_source_fingerprint", ""))
    if recorded_fingerprint:
        return _owned_source_fingerprint(repo, owned_paths) == recorded_fingerprint
    if progress_record.get("version") != 2:
        return False
    # Version 2 did not record content hashes. Its one safe compatibility path requires every
    # owned file to predate the durable stop record as well as the reset generation.
    try:
        return all(
            not (repo / path).is_symlink()
            and (repo / path).is_file()
            and datetime.fromtimestamp((repo / path).stat().st_mtime, timezone.utc)
            <= checkpoint_time
            for path in owned_paths
        )
    except OSError:
        return False


def _rebuilt_generation_supersedes_empty_checkpoint(
    repo: Path,
    progress_record: dict[str, Any],
) -> bool:
    """Prove a newer ClawPatch generation makes a source-clean checkpoint obsolete."""
    if (
        progress_record.get("phase") != "stopped"
        or progress_record.get("owned_paths") != []
        or _source_paths(repo)
    ):
        return False
    state = repo / ".clawpatch"
    project_path = state / "project.json"
    if (
        state.is_symlink()
        or not state.is_dir()
        or project_path.is_symlink()
        or not project_path.is_file()
    ):
        return False
    finding_id = str(progress_record.get("finding_id", ""))
    if (state / "findings" / f"{finding_id}.json").exists():
        return False
    try:
        project = json.loads(project_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(project, dict) or not isinstance(project.get("git"), dict):
        return False
    project_git = project["git"]
    current_branch = _git_text(repo, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    checkpoint_head = str(progress_record.get("head_before", ""))
    generation_head = project_git.get("headSha")
    if (
        progress_record.get("branch") != current_branch
        or project_git.get("currentBranch") != current_branch
        or not isinstance(generation_head, str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", checkpoint_head)
        or not re.fullmatch(r"[0-9a-f]{40,64}", generation_head)
        or not re.fullmatch(r"[0-9a-f]{40,64}", current_head)
    ):
        return False
    generation_time = _parse_checkpoint_time(project.get("createdAt"))
    checkpoint_time = _parse_checkpoint_time(progress_record.get("updated_at"))
    if generation_time is None or checkpoint_time is None or generation_time <= checkpoint_time:
        return False
    checkpoint_to_generation = _run(
        ["git", "merge-base", "--is-ancestor", checkpoint_head, generation_head],
        cwd=repo,
        timeout=60,
    )
    generation_to_current = _run(
        ["git", "merge-base", "--is-ancestor", generation_head, current_head],
        cwd=repo,
        timeout=60,
    )
    return checkpoint_to_generation.returncode == 0 and generation_to_current.returncode == 0


def _committed_clawpatch_config(repo: Path) -> str | None:
    current = repo / ".clawpatch" / "config.json"
    if current.is_file():
        return current.read_text(encoding="utf-8")
    result = _run(
        ["git", "show", "HEAD:.clawpatch/config.json"],
        cwd=repo,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


def _exclude_gitlinks_from_clawpatch_config(repo: Path) -> list[str]:
    """Keep ClawPatch inside the source tree owned by the target repository."""
    gitlinks = _gitlink_paths(repo)
    if not gitlinks:
        return []
    config_path = repo / ".clawpatch" / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SafetyError("Clawpatch config is unreadable after initialization.") from exc
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("exclude"), list)
        or any(not isinstance(pattern, str) for pattern in config.get("exclude", []))
    ):
        raise SafetyError("Clawpatch config has a malformed exclude list.")
    excludes = list(config["exclude"])
    additions: list[str] = []
    for path in gitlinks:
        for pattern in (path, f"{path}/**"):
            if pattern not in excludes:
                excludes.append(pattern)
                additions.append(pattern)
    if additions:
        config["exclude"] = excludes
        atomic_write_json(config_path, config)
    return additions


def _fresh_checkpoint_owned_paths(
    repo: Path,
    source_changes: list[str],
    *,
    state_root: Path | None = None,
) -> list[str]:
    checkpoint = _load_release_progress(repo, state_root=state_root)
    if checkpoint is None or checkpoint.get("phase") not in {"fix", "iteration", "stopped"}:
        return []
    current_branch = _git_text(repo, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if checkpoint["branch"] != current_branch:
        return []
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    if checkpoint["head_before"] != current_head and not _checkpoint_can_follow_supervisor_upgrade(
        repo, checkpoint
    ):
        return []
    owned_paths = set(checkpoint["owned_paths"])
    changed = set(source_changes)
    if not changed or changed != owned_paths:
        return []
    exact_paths = sorted(changed)
    _validate_attempt_paths_syntax(exact_paths)
    if not _checkpoint_proves_exact_source(repo, checkpoint, exact_paths):
        raise SafetyError(
            "A fresh Clawpatch run cannot prove exact checkpoint-owned source content; "
            "preserving ambiguous changes for operator review: " + ", ".join(exact_paths)
        )
    return exact_paths


def _checkpoint_proves_exact_source(
    repo: Path,
    checkpoint: dict[str, Any],
    paths: list[str],
) -> bool:
    exact_paths = sorted(set(paths))
    if (
        not exact_paths
        or exact_paths != sorted(checkpoint.get("owned_paths", []))
        or _source_paths(repo) != exact_paths
    ):
        return False
    recorded_fingerprint = str(checkpoint.get("owned_source_fingerprint", ""))
    if recorded_fingerprint:
        return _owned_source_fingerprint(repo, exact_paths) == recorded_fingerprint
    temporary_commit = str(checkpoint.get("temporary_commit", ""))
    return bool(temporary_commit) and _temporary_commit_matches_owned_source(
        repo,
        original_head=str(checkpoint["head_before"]),
        temporary_commit=temporary_commit,
        paths=exact_paths,
    )


def _preserve_ambiguous_checkpoint_source(
    repo: Path,
    checkpoint: dict[str, Any],
    paths: list[str],
    *,
    state_root: Path,
) -> dict[str, Any] | None:
    """Preserve one exact stale-checkpoint source set and restore current HEAD.

    Recovery is limited to a modern fingerprinted stopped checkpoint whose old
    HEAD is an ancestor of the current HEAD and whose recorded paths exactly
    match every current source change. The ambiguous tree is anchored under a
    local-only Git ref and described by an external receipt before restoration.
    """
    exact_paths = sorted(set(paths))
    owned_paths = sorted(str(path) for path in checkpoint.get("owned_paths", []))
    recorded_fingerprint = str(checkpoint.get("owned_source_fingerprint", ""))
    if (
        checkpoint.get("phase") != "stopped"
        or not recorded_fingerprint
        or not exact_paths
        or exact_paths != owned_paths
        or exact_paths != _source_paths(repo)
    ):
        return None
    _validate_attempt_paths_syntax(exact_paths)
    old_head = str(checkpoint.get("head_before", ""))
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    if not old_head or old_head == current_head:
        return None
    ancestor = _run(
        ["git", "merge-base", "--is-ancestor", old_head, current_head],
        cwd=repo,
        timeout=60,
    )
    if ancestor.returncode:
        return None

    temporary_root = current_temporary_root()
    with tempfile.TemporaryDirectory(
        prefix="clawpatch-supervise-recovery-index-",
        dir=str(temporary_root) if temporary_root is not None else None,
    ) as temp:
        index_path = Path(temp) / "index"
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index_path)
        env.update(
            {
                "GIT_AUTHOR_NAME": "ClawPatch Supervise Recovery",
                "GIT_AUTHOR_EMAIL": "clawpatch-supervise-recovery@localhost",
                "GIT_COMMITTER_NAME": "ClawPatch Supervise Recovery",
                "GIT_COMMITTER_EMAIL": "clawpatch-supervise-recovery@localhost",
            }
        )
        _must_run(["git", "read-tree", current_head], cwd=repo, timeout=120, env=env)
        _must_run(["git", "add", "-A", "--", *exact_paths], cwd=repo, timeout=120, env=env)
        preserved_tree = _must_run(
            ["git", "write-tree"], cwd=repo, timeout=120, env=env
        ).strip()
        preserved_commit = _must_run(
            [
                "git",
                "commit-tree",
                preserved_tree,
                "-p",
                current_head,
                "-m",
                "clawpatch-supervise recovery: preserve ambiguous checkpoint source",
            ],
            cwd=repo,
            timeout=120,
            env=env,
        ).strip()
    if _paths_between(repo, current_head, preserved_commit) != exact_paths:
        raise SafetyError(
            "Automatic Clawpatch checkpoint recovery could not preserve exactly its ambiguous "
            "source paths."
        )

    preserved_ref = f"refs/clawpatch-supervise/recovery/{preserved_commit}"
    existing_ref = _run(
        ["git", "rev-parse", "--verify", preserved_ref],
        cwd=repo,
        timeout=60,
    )
    if existing_ref.returncode:
        _must_run(
            ["git", "update-ref", preserved_ref, preserved_commit],
            cwd=repo,
            timeout=60,
        )
    elif existing_ref.stdout.strip() != preserved_commit:
        raise SafetyError("Clawpatch recovery ref unexpectedly points to different source.")

    receipt_path = state_root / "recoveries" / f"{preserved_commit}.json"
    receipt = {
        "version": 1,
        "repo": str(repo.resolve()),
        "finding_id": str(checkpoint["finding_id"]),
        "checkpoint_head": old_head,
        "current_head": current_head,
        "paths": exact_paths,
        "recorded_source_fingerprint": recorded_fingerprint,
        "preserved_source_fingerprint": _source_paths_fingerprint(repo, exact_paths),
        "preserved_commit": preserved_commit,
        "preserved_ref": preserved_ref,
        "created_at": utc_now(),
        "checkpoint": checkpoint,
    }
    atomic_write_json(receipt_path, receipt)

    _discard_checkpoint_owned_source(repo, exact_paths)
    remaining_source = _source_paths(repo)
    if remaining_source:
        raise SafetyError(
            "Automatic Clawpatch checkpoint recovery preserved source at "
            f"{preserved_ref} but could not restore a clean current HEAD: "
            + ", ".join(remaining_source)
        )
    _clear_release_progress(repo, state_root=state_root)
    return {
        "finding_id": str(checkpoint["finding_id"]),
        "paths": exact_paths,
        "preserved_commit": preserved_commit,
        "preserved_ref": preserved_ref,
        "receipt": str(receipt_path),
    }


def _temporary_commit_matches_owned_source(
    repo: Path,
    *,
    original_head: str,
    temporary_commit: str,
    paths: list[str],
) -> bool:
    """Compare dirty source with a recognized iteration commit without changing Git state."""
    temporary_root = current_temporary_root()
    with tempfile.TemporaryDirectory(
        prefix="manageroo-clawpatch-index-",
        dir=str(temporary_root) if temporary_root is not None else None,
    ) as temp:
        index_path = Path(temp) / "index"
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index_path)
        _must_run(["git", "read-tree", original_head], cwd=repo, timeout=120, env=env)
        _must_run(["git", "add", "-A", "--", *paths], cwd=repo, timeout=120, env=env)
        actual_tree = _must_run(["git", "write-tree"], cwd=repo, timeout=120, env=env).strip()
    expected_tree = _git_text(repo, ["git", "rev-parse", f"{temporary_commit}^{{tree}}"])
    return actual_tree == expected_tree


def _recover_checkpoint_temporary_commit(
    repo: Path,
    *,
    state_root: Path | None = None,
) -> None:
    checkpoint = _load_release_progress(repo, state_root=state_root)
    if checkpoint is None or not checkpoint.get("temporary_commit"):
        return
    finding_id = str(checkpoint["finding_id"])
    original_head = str(checkpoint["head_before"])
    temporary_commit = str(checkpoint["temporary_commit"])
    current_branch = _git_text(repo, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if checkpoint["branch"] != current_branch:
        raise SafetyError("Interrupted Clawpatch temporary commit belongs to a different branch.")
    owned_paths = _verify_iteration_commit(
        repo,
        finding_id=finding_id,
        original_head=original_head,
        temporary_commit=temporary_commit,
        require_current=False,
    )
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    source_changes = _source_paths(repo)
    if current_head == original_head and not source_changes:
        # The stopped iteration was already returned to its recorded base and no
        # repair remains in the worktree. A fresh run may safely retire the
        # verified dangling commit even when an older checkpoint recorded stale
        # or empty owned paths.
        return
    if owned_paths != sorted(checkpoint["owned_paths"]):
        raise SafetyError(
            "Interrupted Clawpatch temporary commit paths do not match its checkpoint."
        )
    if current_head == temporary_commit:
        if source_changes:
            raise SafetyError(
                "Interrupted Clawpatch temporary commit has additional uncheckpointed source "
                "changes: " + ", ".join(source_changes)
            )
        _must_run(["git", "reset", "--mixed", original_head], cwd=repo, timeout=120)
    elif current_head != original_head:
        original_is_ancestor = _run(
            ["git", "merge-base", "--is-ancestor", original_head, current_head],
            cwd=repo,
            timeout=60,
        )
        temporary_is_ancestor = _run(
            ["git", "merge-base", "--is-ancestor", temporary_commit, current_head],
            cwd=repo,
            timeout=60,
        )
        if (
            not source_changes
            and original_is_ancestor.returncode == 0
            and temporary_is_ancestor.returncode == 1
        ):
            return
        raise SafetyError(
            "Interrupted Clawpatch temporary commit no longer matches the current Git HEAD."
        )
    recovered_paths = _source_paths(repo)
    if recovered_paths != owned_paths:
        raise SafetyError(
            "Recovered Clawpatch temporary commit does not expose exactly its checkpoint paths."
        )


def _validate_attempt_paths_syntax(paths: list[str]) -> None:
    invalid = []
    for path in paths:
        posix = PurePosixPath(path)
        windows = PureWindowsPath(path)
        if (
            not path
            or posix.is_absolute()
            or windows.is_absolute()
            or ".." in posix.parts
            or ".." in windows.parts
            or path == ".clawpatch"
            or path.startswith(".clawpatch/")
        ):
            invalid.append(path)
    if invalid:
        raise SafetyError(
            "Clawpatch patch attempt contains unsafe or state-only paths: " + ", ".join(invalid)
        )


def _discard_checkpoint_owned_source(repo: Path, paths: list[str]) -> None:
    tracked: list[str] = []
    untracked: list[str] = []
    for path in paths:
        output = _must_run(
            ["git", "ls-tree", "--name-only", "-z", "HEAD", "--", path],
            cwd=repo,
            timeout=60,
        )
        if path in output.split("\0"):
            tracked.append(path)
        else:
            untracked.append(path)
    if tracked:
        _must_run(
            ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", *tracked],
            cwd=repo,
            timeout=120,
        )
    for path in untracked:
        candidate = repo / path
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
            continue
        if candidate.exists():
            raise SafetyError(
                f"A fresh Clawpatch run cannot safely discard non-file path {path!r}."
            )
    remaining = _source_paths(repo)
    if remaining:
        raise SafetyError(
            "A fresh Clawpatch run could not verify exact cleanup of the interrupted repair: "
            + ", ".join(remaining)
        )


def _prepare_fresh_release(
    repo: Path,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    state_root: Path | None = None,
) -> None:
    """Delete only Clawpatch run state, preserve project configuration, and initialize again."""
    _require_no_process(repo)
    try:
        _recover_checkpoint_temporary_commit(repo, state_root=state_root)
    except SafetyError as exc:
        malformed = "release progress is malformed" in str(exc)
        current_message = _git_text(repo, ["git", "show", "-s", "--format=%s", "HEAD"])
        if (
            not malformed
            or _source_paths(repo)
            or current_message.startswith("manageroo clawpatch iteration:")
            or current_message.startswith("clawpatch-supervise iteration:")
        ):
            raise
    clawpatch_state_root = repo / ".clawpatch"
    if clawpatch_state_root.is_symlink() or clawpatch_state_root.resolve().parent != repo.resolve():
        raise SafetyError("The .clawpatch state path is not a safe repository-owned directory.")
    source_changes = _source_paths(repo)
    if source_changes:
        raise SafetyError(
            "A fresh Clawpatch reset is allowed only when project source is clean. "
            "The supervisor preserved .clawpatch, its checkpoint, and these source changes: "
            + ", ".join(source_changes)
        )
    config_text = _committed_clawpatch_config(repo)
    if clawpatch_state_root.exists():
        if not clawpatch_state_root.is_dir():
            raise SafetyError("The .clawpatch state path is not a directory.")
        shutil.rmtree(clawpatch_state_root)
    _clear_release_progress(repo, state_root=state_root)
    proof_root = state_root if state_root is not None else repo / PROJECT_DIR / "cache"
    (proof_root / "clawpatch-release-proof.json").unlink(missing_ok=True)
    if progress is not None:
        progress(
            {
                "phase": "fresh",
                "current": "?",
                "total": "?",
                "command": "clawpatch init --json",
                "attempt": 1,
                "max_attempts": 1,
            }
        )
    _json_clawpatch(
        repo,
        ["clawpatch", "init", "--json"],
        env=env,
        progress=None,
    )
    if config_text is not None:
        config_path = clawpatch_state_root / "config.json"
        config_path.write_text(config_text, encoding="utf-8")
    excluded_gitlinks = _exclude_gitlinks_from_clawpatch_config(repo)
    if excluded_gitlinks and progress is not None:
        progress(
            {
                "phase": "submodule-exclusion",
                "current": "?",
                "total": "?",
                "command": "exclude unowned Git submodules from ClawPatch mapping",
                "detail": ", ".join(excluded_gitlinks),
            }
        )


def _commit_attempt(
    repo: Path,
    finding_id: str,
    files: list[str],
    *,
    branch: str,
    outcome: str = "fixed",
) -> str:
    if not files:
        return ""
    _require_branch(repo, branch, phase="source commit")
    _validate_attempt_paths(repo, files)
    _must_run(["git", "add", "--", *files], cwd=repo, timeout=120)
    staged = _must_run(
        ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"], cwd=repo, timeout=120
    )
    staged_paths = sorted(path for path in staged.split("\0") if path)
    reported_paths = set(files)
    if any(path not in reported_paths or path.startswith(".clawpatch/") for path in staged_paths):
        raise SafetyError(
            "The staged paths do not exactly match the current Clawpatch patch attempt."
        )
    if _source_paths(repo) != staged_paths:
        raise SafetyError("The staged repair does not contain every current source change.")
    if not staged_paths:
        return ""
    _must_run(["git", "diff", "--cached", "--check"], cwd=repo, timeout=120)
    _require_branch(repo, branch, phase="source commit")
    commit_kind = "continuation" if outcome == "open" else "fix"
    _commit_without_local_hooks(repo, "-m", f"clawpatch {commit_kind}: {finding_id}")
    commit = _git_text(repo, ["git", "rev-parse", "HEAD"])
    committed = _git_text(
        repo, ["git", "show", "--pretty=", "--name-only", "--no-renames", commit]
    ).splitlines()
    if sorted(path for path in committed if path) != staged_paths:
        raise SafetyError(
            "The resulting commit does not contain exactly the verified source repair."
        )
    return commit


def _commit_without_local_hooks(repo: Path, *args: str) -> None:
    temporary_root = current_temporary_root()
    with tempfile.TemporaryDirectory(
        prefix="manageroo-empty-hooks-",
        dir=str(temporary_root) if temporary_root is not None else None,
    ) as hooks_path:
        _must_run(
            [
                "git",
                "-c",
                "commit.gpgSign=false",
                "-c",
                f"core.hooksPath={hooks_path}",
                "commit",
                *args,
            ],
            cwd=repo,
            timeout=300,
        )


def _paths_between(repo: Path, start: str, end: str = "HEAD") -> list[str]:
    output = _must_run(
        [
            "git",
            "diff",
            "--name-only",
            "--no-renames",
            "--diff-filter=ACDMRT",
            "-z",
            f"{start}..{end}",
        ],
        cwd=repo,
        timeout=120,
    )
    paths = sorted(path for path in output.split("\0") if path)
    _validate_attempt_paths_syntax(paths)
    return paths


def _verify_iteration_commit(
    repo: Path,
    *,
    finding_id: str,
    original_head: str,
    temporary_commit: str,
    require_current: bool = True,
) -> list[str]:
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    if require_current and current_head != temporary_commit:
        raise SafetyError(
            "Clawpatch iteration history no longer matches the supervisor temporary commit."
        )
    parent = _git_text(repo, ["git", "rev-parse", f"{temporary_commit}^"])
    if parent != original_head:
        raise SafetyError(
            "Clawpatch temporary iteration commit is not based directly on the finding start."
        )
    message = _git_text(repo, ["git", "show", "-s", "--format=%s", temporary_commit])
    allowed_messages = {
        f"clawpatch-supervise iteration: {finding_id}",
        f"manageroo clawpatch iteration: {finding_id}",
        f"clawpatch fix: {finding_id}",
    }
    if message not in allowed_messages:
        raise SafetyError("Clawpatch temporary iteration commit has an unrecognized identity.")
    return _paths_between(repo, original_head, temporary_commit)


def _stage_current_source(repo: Path) -> tuple[list[str], str]:
    paths = _source_paths(repo)
    if not paths:
        return [], _git_text(repo, ["git", "rev-parse", "HEAD^{tree}"])
    _validate_attempt_paths_syntax(paths)
    existing = _must_run(
        ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"],
        cwd=repo,
        timeout=120,
    )
    if any(existing.split("\0")):
        raise SafetyError("Clawpatch iteration found pre-existing staged source changes.")
    _must_run(["git", "add", "--", *paths], cwd=repo, timeout=120)
    staged = sorted(
        path
        for path in _must_run(
            ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"],
            cwd=repo,
            timeout=120,
        ).split("\0")
        if path
    )
    if staged != paths:
        raise SafetyError("The staged iteration does not exactly match Clawpatch source changes.")
    _must_run(["git", "diff", "--cached", "--check"], cwd=repo, timeout=120)
    return paths, _git_text(repo, ["git", "write-tree"])


def _save_partial_iteration(
    repo: Path,
    *,
    finding_id: str,
    branch: str,
    original_head: str,
    temporary_commit: str,
    seen_states: set[str],
    state_root: Path,
) -> tuple[str, list[str], str]:
    _require_branch(repo, branch, phase="partial iteration")
    paths, source_state = _stage_current_source(repo)
    if not paths:
        raise _UnresolvedFinding(
            "Clawpatch made no source changes, so another identical fix call cannot progress.",
            finding_id=finding_id,
            outcome="no-progress",
        )
    original_state = _git_text(repo, ["git", "rev-parse", f"{original_head}^{{tree}}"])
    if source_state == original_state or source_state in seen_states:
        raise _UnresolvedFinding(
            "Clawpatch produced a source-tree state already seen for this finding.",
            finding_id=finding_id,
            outcome="no-progress",
        )
    if temporary_commit:
        _verify_iteration_commit(
            repo,
            finding_id=finding_id,
            original_head=original_head,
            temporary_commit=temporary_commit,
        )
        _commit_without_local_hooks(repo, "--amend", "--no-edit")
    else:
        current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
        if current_head != original_head:
            raise SafetyError("Git history changed before the first Clawpatch partial iteration.")
        _commit_without_local_hooks(
            repo,
            "-m",
            f"clawpatch-supervise iteration: {finding_id}",
        )
    temporary_commit = _git_text(repo, ["git", "rev-parse", "HEAD"])
    owned_paths = _verify_iteration_commit(
        repo,
        finding_id=finding_id,
        original_head=original_head,
        temporary_commit=temporary_commit,
    )
    if _source_paths(repo):
        raise SafetyError("Clawpatch temporary iteration commit did not leave source clean.")
    seen_states.add(source_state)
    _write_release_progress(
        repo,
        finding_id=finding_id,
        branch=branch,
        head_before=original_head,
        phase="iteration",
        owned_paths=owned_paths,
        temporary_commit=temporary_commit,
        source_states=sorted(seen_states),
        last_action=RepairAction.PRESERVE_AND_CONTINUE,
        state_root=state_root,
    )
    return temporary_commit, owned_paths, source_state


def _finalize_finding_commit(
    repo: Path,
    *,
    finding_id: str,
    branch: str,
    original_head: str,
    temporary_commit: str,
    seen_states: set[str],
) -> str:
    _require_branch(repo, branch, phase="final finding commit")
    if temporary_commit:
        _verify_iteration_commit(
            repo,
            finding_id=finding_id,
            original_head=original_head,
            temporary_commit=temporary_commit,
        )
        paths, source_state = _stage_current_source(repo)
        if paths and (
            source_state == _git_text(repo, ["git", "rev-parse", f"{original_head}^{{tree}}"])
            or source_state in seen_states
        ):
            raise _UnresolvedFinding(
                "Clawpatch final attempt repeated an earlier source-tree state.",
                finding_id=finding_id,
                outcome="no-progress",
            )
        _commit_without_local_hooks(
            repo,
            "--amend",
            "-m",
            f"clawpatch fix: {finding_id}",
        )
        commit = _git_text(repo, ["git", "rev-parse", "HEAD"])
    else:
        files = _source_paths(repo)
        commit = _commit_attempt(repo, finding_id, files, branch=branch, outcome="fixed")
    if not commit:
        raise _UnresolvedFinding(
            "Clawpatch reported fixed without producing a source repair.",
            finding_id=finding_id,
            outcome="fixed-no-progress",
        )
    parent = _git_text(repo, ["git", "rev-parse", f"{commit}^"])
    count = _git_text(repo, ["git", "rev-list", "--count", f"{original_head}..{commit}"])
    if parent != original_head or count != "1":
        raise SafetyError(
            "A repaired finding must produce exactly one commit above its start HEAD."
        )
    committed_paths = _paths_between(repo, original_head, commit)
    if not committed_paths or _source_paths(repo):
        raise SafetyError("The final Clawpatch commit is empty or left source changes behind.")
    return commit


def _stop_finding_iteration(
    repo: Path,
    *,
    finding_id: str,
    branch: str,
    original_head: str,
    temporary_commit: str,
    seen_states: set[str],
    state_root: Path,
    repair_action: RepairAction = RepairAction.STOP_TERMINAL,
) -> list[str]:
    if temporary_commit:
        _verify_iteration_commit(
            repo,
            finding_id=finding_id,
            original_head=original_head,
            temporary_commit=temporary_commit,
        )
        _must_run(["git", "reset", "--mixed", original_head], cwd=repo, timeout=120)
    else:
        current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
        if current_head == original_head:
            _must_run(["git", "reset", "--mixed", original_head], cwd=repo, timeout=120)
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    if current_head != original_head:
        raise SafetyError("Clawpatch safe stop could not restore the finding start HEAD.")
    owned_paths = _source_paths(repo)
    _validate_attempt_paths_syntax(owned_paths)
    _write_release_progress(
        repo,
        finding_id=finding_id,
        branch=branch,
        head_before=original_head,
        phase="stopped",
        owned_paths=owned_paths,
        temporary_commit=temporary_commit,
        source_states=sorted(seen_states),
        last_action=repair_action,
        state_root=state_root,
    )
    return owned_paths


def _complete_fixed_finding(
    repo: Path,
    finding_id: str,
    *,
    record: dict[str, Any],
    branch: str,
    original_head: str,
    temporary_commit: str,
    seen_states: set[str],
    state_root: Path,
    push_mode: str,
    pushed: bool,
    continuations: int,
    progress: Callable[[dict[str, Any]], None] | None,
    current: int | str,
    total: int | str,
) -> tuple[dict[str, Any], bool, int]:
    no_commit_required = not temporary_commit and not _source_paths(repo)
    if no_commit_required and _git_text(repo, ["git", "rev-parse", "HEAD"]) != original_head:
        raise SafetyError(
            "Git history changed while Clawpatch fixed a finding without source changes."
        )
    if progress is not None and not no_commit_required:
        commit_command = (
            f"git commit --amend -m 'clawpatch fix: {finding_id}'"
            if temporary_commit
            else f"git commit -m 'clawpatch fix: {finding_id}'"
        )
        progress(
            {
                "phase": "commit",
                "current": current,
                "total": total,
                "finding_id": finding_id,
                "command": commit_command,
                "attempt": 1,
                "max_attempts": 1,
            }
        )
    if no_commit_required:
        commit = ""
    else:
        try:
            commit = _finalize_finding_commit(
                repo,
                finding_id=finding_id,
                branch=branch,
                original_head=original_head,
                temporary_commit=temporary_commit,
                seen_states=seen_states,
            )
        except BaseException:
            _stop_finding_iteration(
                repo,
                finding_id=finding_id,
                branch=branch,
                original_head=original_head,
                temporary_commit=temporary_commit,
                seen_states=seen_states,
                state_root=state_root,
            )
            raise
    record["head_before"] = original_head
    record["files_changed"] = _paths_between(repo, original_head, commit) if commit else []
    record["commit"] = commit
    _write_release_progress(
        repo,
        finding_id=finding_id,
        branch=branch,
        head_before=original_head,
        phase="finalized",
        owned_paths=list(record["files_changed"]),
        source_states=sorted(seen_states),
        state_root=state_root,
    )
    if push_mode == "each" and commit:
        if progress is not None:
            push_argv = (
                f"git push -u origin {branch}" if not pushed else f"git push origin {branch}"
            )
            progress(
                {
                    "phase": "push",
                    "current": current,
                    "total": total,
                    "finding_id": finding_id,
                    "command": push_argv,
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
        _push_and_verify(repo, branch, first=not pushed)
        pushed = True
    _clear_release_progress(repo, state_root=state_root)
    return record, pushed, continuations


def _process_finding_until_fixed(
    repo: Path,
    finding_id: str,
    *,
    inspected: dict[str, Any],
    env: dict[str, str],
    push_mode: str,
    branch: str,
    pushed: bool,
    state_root: Path,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
    require_project_gates: bool = True,
    resume_original_head: str = "",
    resume_temporary_commit: str = "",
    resume_seen_states: set[str] | None = None,
    resume_attempt: int = 1,
    resume_continuations: int = 0,
    advance_uncertain: bool = False,
) -> tuple[dict[str, Any], bool, int]:
    original_head = resume_original_head or _git_text(repo, ["git", "rev-parse", "HEAD"])
    temporary_commit = resume_temporary_commit
    seen_states = (
        set(resume_seen_states)
        if resume_seen_states is not None
        else {_git_text(repo, ["git", "rev-parse", f"{original_head}^{{tree}}"])}
    )
    attempt = resume_attempt
    continuations = resume_continuations
    zero_source_retries = 0
    if attempt < 1 or continuations < 0:
        raise SafetyError("Invalid resumed Clawpatch iteration counters.")
    if temporary_commit:
        _verify_iteration_commit(
            repo,
            finding_id=finding_id,
            original_head=original_head,
            temporary_commit=temporary_commit,
        )
        if _source_paths(repo):
            raise SafetyError("Resumed Clawpatch iteration must start from a clean source tree.")
    elif _source_paths(repo):
        raise SafetyError("Pre-existing source changes block the current Clawpatch finding.")
    while True:
        _write_release_progress(
            repo,
            finding_id=finding_id,
            branch=branch,
            head_before=original_head,
            phase="fix",
            owned_paths=(
                _paths_between(repo, original_head, temporary_commit) if temporary_commit else []
            ),
            temporary_commit=temporary_commit,
            source_states=sorted(seen_states),
            state_root=state_root,
        )
        if progress is not None:
            progress(
                {
                    "phase": "fix",
                    "current": current,
                    "total": total,
                    "finding_id": finding_id,
                    "attempt": attempt,
                    "command": f"clawpatch fix --finding {finding_id}",
                }
            )
        try:
            record, _unused_pushed = _execute_fix(
                repo,
                finding_id,
                inspected=inspected,
                env=env,
                push_mode="none",
                branch=branch,
                pushed=False,
                progress=progress,
                current=current,
                total=total,
                require_project_gates=require_project_gates,
                finalize=False,
            )
        except _UnresolvedFinding as exc:
            failure = exc.failure or failure_from_legacy_outcome(exc.outcome)
            has_source_progress = bool(_source_paths(repo))
            if (
                failure is not None
                and failure.phase == "fix"
                and failure.kind is ClawpatchFailureKind.VALIDATION_FAILED
                and not has_source_progress
            ):
                try:
                    gate_runs = _run_project_gates(
                        repo,
                        finding_id=finding_id,
                        required=require_project_gates,
                    )
                    validation = _revalidate(
                        repo,
                        finding_id,
                        env=env,
                        expected_paths=[],
                        progress=progress,
                        current=current,
                        total=total,
                    )
                except BaseException:
                    _stop_finding_iteration(
                        repo,
                        finding_id=finding_id,
                        branch=branch,
                        original_head=original_head,
                        temporary_commit=temporary_commit,
                        seen_states=seen_states,
                        state_root=state_root,
                    )
                    raise
                if validation.get("outcome") == "fixed":
                    record = {
                        "finding_id": finding_id,
                        "inspection": inspected,
                        "head_before": original_head,
                        "patch_attempt": "existing-repair-revalidation",
                        "files_changed": (
                            _paths_between(repo, original_head, temporary_commit)
                            if temporary_commit
                            else []
                        ),
                        "gate_runs": gate_runs,
                        "revalidation": validation,
                        "commit": "",
                    }
                    return _complete_fixed_finding(
                        repo,
                        finding_id,
                        record=record,
                        branch=branch,
                        original_head=original_head,
                        temporary_commit=temporary_commit,
                        seen_states=seen_states,
                        state_root=state_root,
                        push_mode=push_mode,
                        pushed=pushed,
                        continuations=continuations,
                        progress=progress,
                        current=current,
                        total=total,
                    )
            if failure is None:
                action = RepairAction.STOP_TERMINAL
            else:
                action = decide_repair_transition(
                    failure=failure,
                    has_source_progress=has_source_progress,
                ).action
            exc.repair_action = action
            if action is not RepairAction.PRESERVE_AND_CONTINUE:
                _stop_finding_iteration(
                    repo,
                    finding_id=finding_id,
                    branch=branch,
                    original_head=original_head,
                    temporary_commit=temporary_commit,
                    seen_states=seen_states,
                    state_root=state_root,
                    repair_action=action,
                )
                if failure is not None and failure.progress_capable and not has_source_progress:
                    raise _UnresolvedFinding(
                        "Clawpatch iteration produced no source changes; same-finding "
                        f"continuation is not allowed.\nOriginal Clawpatch failure: {exc}",
                        finding_id=finding_id,
                        outcome="no-progress",
                        failure=failure,
                        repair_action=action,
                    ) from exc
                raise
            try:
                temporary_commit, _owned_paths, _state = _save_partial_iteration(
                    repo,
                    finding_id=finding_id,
                    branch=branch,
                    original_head=original_head,
                    temporary_commit=temporary_commit,
                    seen_states=seen_states,
                    state_root=state_root,
                )
            except _UnresolvedFinding as progress_exc:
                _stop_finding_iteration(
                    repo,
                    finding_id=finding_id,
                    branch=branch,
                    original_head=original_head,
                    temporary_commit=temporary_commit,
                    seen_states=seen_states,
                    state_root=state_root,
                )
                if progress_exc.outcome == "no-progress":
                    raise _UnresolvedFinding(
                        f"{progress_exc}\nOriginal Clawpatch failure: {exc}",
                        finding_id=finding_id,
                        outcome="no-progress",
                    ) from exc
                raise
            except BaseException:
                _stop_finding_iteration(
                    repo,
                    finding_id=finding_id,
                    branch=branch,
                    original_head=original_head,
                    temporary_commit=temporary_commit,
                    seen_states=seen_states,
                    state_root=state_root,
                )
                raise
            continuations += 1
            zero_source_retries = 0
            if failure is not None and failure.phase == "fix" and failure.progress_capable:
                try:
                    gate_runs = _run_project_gates(
                        repo,
                        finding_id=finding_id,
                        required=require_project_gates,
                    )
                    validation = _revalidate(
                        repo,
                        finding_id,
                        env=env,
                        expected_paths=[],
                        progress=progress,
                        current=current,
                        total=total,
                    )
                except BaseException:
                    _stop_finding_iteration(
                        repo,
                        finding_id=finding_id,
                        branch=branch,
                        original_head=original_head,
                        temporary_commit=temporary_commit,
                        seen_states=seen_states,
                        state_root=state_root,
                    )
                    raise
                record = {
                    "finding_id": finding_id,
                    "inspection": inspected,
                    "head_before": original_head,
                    "patch_attempt": "saved-repair-recovery",
                    "files_changed": _paths_between(repo, original_head, temporary_commit),
                    "gate_runs": gate_runs,
                    "revalidation": validation,
                    "commit": "",
                }
                if validation.get("outcome") == "open" or (
                    validation.get("outcome") == "uncertain" and not advance_uncertain
                ):
                    validation_outcome = str(validation["outcome"])
                    if progress is not None:
                        progress(
                            {
                                "phase": "continuing",
                                "current": current,
                                "total": total,
                                "finding_id": finding_id,
                                "commit": temporary_commit,
                                "detail": (
                                    f"{failure.kind.value} left a saved repair; fresh "
                                    f"revalidation returned {validation_outcome} evidence, "
                                    "and the next fix will use it"
                                ),
                            }
                        )
                    attempt += 1
                    continue
            else:
                if progress is not None:
                    progress(
                        {
                            "phase": "continuing",
                            "current": current,
                            "total": total,
                            "finding_id": finding_id,
                            "commit": temporary_commit,
                            "detail": (
                                "revalidation source progress preserved locally; continuing same finding"
                                if exc.outcome
                                in {
                                    "revalidation-command-failed-with-source-progress",
                                    "revalidation-mutated-source",
                                    "revalidation-provider-failed",
                                }
                                else "partial repair preserved locally; continuing same finding"
                            ),
                        }
                    )
                attempt += 1
                continue
        except BaseException:
            _stop_finding_iteration(
                repo,
                finding_id=finding_id,
                branch=branch,
                original_head=original_head,
                temporary_commit=temporary_commit,
                seen_states=seen_states,
                state_root=state_root,
            )
            raise
        revalidation_outcome = str(record.get("revalidation", {}).get("outcome", ""))
        try:
            revalidation_decision = decide_repair_transition(
                revalidation_outcome=revalidation_outcome,
                has_source_progress=bool(temporary_commit or _source_paths(repo)),
                advance_uncertain=advance_uncertain,
            )
        except ValueError as exc:
            _stop_finding_iteration(
                repo,
                finding_id=finding_id,
                branch=branch,
                original_head=original_head,
                temporary_commit=temporary_commit,
                seen_states=seen_states,
                state_root=state_root,
            )
            raise SafetyError("Clawpatch returned an unsupported revalidation outcome.") from exc
        if revalidation_decision.action is RepairAction.COMMIT_AND_ADVANCE:
            record["deferred_uncertain"] = True
            return _complete_fixed_finding(
                repo,
                finding_id,
                record=record,
                branch=branch,
                original_head=original_head,
                temporary_commit=temporary_commit,
                seen_states=seen_states,
                state_root=state_root,
                push_mode=push_mode,
                pushed=pushed,
                continuations=continuations,
                progress=progress,
                current=current,
                total=total,
            )
        if revalidation_decision.action is RepairAction.PRESERVE_AND_CONTINUE:
            if (
                not _source_paths(repo)
                and zero_source_retries < CLAWPATCH_ZERO_SOURCE_RETRY_LIMIT
            ):
                zero_source_retries += 1
                attempt += 1
                if progress is not None:
                    progress(
                        {
                            "phase": "continuing",
                            "current": current,
                            "total": total,
                            "finding_id": finding_id,
                            "commit": temporary_commit,
                            "detail": (
                                f"{revalidation_outcome} revalidation supplied new evidence; "
                                "retrying the same finding without advancing the queue"
                            ),
                            "evidence_retry": zero_source_retries,
                            "max_evidence_retries": CLAWPATCH_ZERO_SOURCE_RETRY_LIMIT,
                        }
                    )
                continue
            try:
                temporary_commit, _owned_paths, _state = _save_partial_iteration(
                    repo,
                    finding_id=finding_id,
                    branch=branch,
                    original_head=original_head,
                    temporary_commit=temporary_commit,
                    seen_states=seen_states,
                    state_root=state_root,
                )
            except BaseException:
                _stop_finding_iteration(
                    repo,
                    finding_id=finding_id,
                    branch=branch,
                    original_head=original_head,
                    temporary_commit=temporary_commit,
                    seen_states=seen_states,
                    state_root=state_root,
                )
                raise
            continuations += 1
            zero_source_retries = 0
            if progress is not None:
                progress(
                    {
                        "phase": "continuing",
                        "current": current,
                        "total": total,
                        "finding_id": finding_id,
                        "commit": temporary_commit,
                        "detail": (
                            f"{revalidation_outcome} repair preserved locally; "
                            "continuing same finding"
                        ),
                    }
                )
            attempt += 1
            continue
        if revalidation_decision.action is RepairAction.DISCARD_AND_CONTINUE:
            exact_repair_paths = {
                str(path) for path in record.get("files_changed", []) if isinstance(path, str)
            }
            if temporary_commit:
                exact_repair_paths.update(_paths_between(repo, original_head, temporary_commit))
            stopped_paths = _stop_finding_iteration(
                repo,
                finding_id=finding_id,
                branch=branch,
                original_head=original_head,
                temporary_commit=temporary_commit,
                seen_states=seen_states,
                state_root=state_root,
                repair_action=RepairAction.DISCARD_AND_CONTINUE,
            )
            if not set(stopped_paths).issubset(exact_repair_paths):
                raise SafetyError(
                    "ClawPatch false-positive cleanup found source paths outside the exact "
                    "supervisor-owned repair."
                )
            if stopped_paths:
                _discard_checkpoint_owned_source(repo, stopped_paths)
            _clear_release_progress(repo, state_root=state_root)
            record["discarded_paths"] = sorted(exact_repair_paths)
            record["files_changed"] = []
            record["commit"] = ""
            record["false_positive"] = True
            if progress is not None:
                progress(
                    {
                        "phase": "false-positive",
                        "current": current,
                        "total": total,
                        "finding_id": finding_id,
                        "commit": "",
                        "detail": (
                            "ClawPatch classified the finding false-positive; restored only "
                            "the exact supervisor-owned repair paths and continued the queue"
                        ),
                    }
                )
            return record, pushed, continuations
        if revalidation_decision.action is not RepairAction.FINALIZE:
            _stop_finding_iteration(
                repo,
                finding_id=finding_id,
                branch=branch,
                original_head=original_head,
                temporary_commit=temporary_commit,
                seen_states=seen_states,
                state_root=state_root,
                repair_action=revalidation_decision.action,
            )
            raise SafetyError("Clawpatch returned an unsupported revalidation outcome.")
        return _complete_fixed_finding(
            repo,
            finding_id,
            record=record,
            branch=branch,
            original_head=original_head,
            temporary_commit=temporary_commit,
            seen_states=seen_states,
            state_root=state_root,
            push_mode=push_mode,
            pushed=pushed,
            continuations=continuations,
            progress=progress,
            current=current,
            total=total,
        )


def _push_and_verify(repo: Path, branch: str, *, first: bool) -> None:
    _require_branch(repo, branch, phase="push")
    argv = ["git", "push", "-u", "origin", branch] if first else ["git", "push", "origin", branch]
    _must_run(argv, cwd=repo, timeout=600)
    local = _git_text(repo, ["git", "rev-parse", "HEAD"])
    remote_line = _git_text(repo, ["git", "ls-remote", "origin", f"refs/heads/{branch}"])
    remote = remote_line.split()[0] if remote_line else ""
    if remote != local:
        raise SafetyError(f"Live remote branch SHA {remote!r} does not equal local HEAD {local!r}.")


def _publish_final_state(repo: Path, *, branch: str) -> str:
    state_paths = [
        path
        for path in _status_paths(repo)
        if path == ".clawpatch" or path.startswith(".clawpatch/")
    ]
    if not state_paths:
        return ""
    _require_branch(repo, branch, phase="state publication")
    _must_run(["git", "add", "-A", "--", *state_paths], cwd=repo, timeout=120)
    staged = sorted(
        path
        for path in _must_run(
            ["git", "diff", "--cached", "--name-only", "--no-renames", "-z"], cwd=repo, timeout=120
        ).split("\0")
        if path
    )
    if staged != sorted(state_paths) or any(not path.startswith(".clawpatch/") for path in staged):
        raise SafetyError(
            "Final state commit is not exactly limited to authorized .clawpatch paths."
        )
    _must_run(["git", "commit", "-m", "clawpatch state: final closure"], cwd=repo, timeout=300)
    return _git_text(repo, ["git", "rev-parse", "HEAD"])


def _restore_committed_clawpatch_state(repo: Path) -> None:
    state_root = repo / ".clawpatch"
    if state_root.is_symlink() or state_root.resolve().parent != repo.resolve():
        raise SafetyError("Final Clawpatch state cleanup requires a safe repository directory.")
    before_source = _source_state_fingerprint(repo)
    if state_root.exists():
        if not state_root.is_dir():
            raise SafetyError("Final Clawpatch state path is not a directory.")
        shutil.rmtree(state_root)
    tracked = [
        path
        for path in _must_run(
            ["git", "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", ".clawpatch"],
            cwd=repo,
            timeout=120,
        ).split("\0")
        if path
    ]
    if tracked:
        _must_run(
            ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", ".clawpatch"],
            cwd=repo,
            timeout=120,
        )
    remaining_state = [
        path
        for path in _status_paths(repo)
        if path == ".clawpatch" or path.startswith(".clawpatch/")
    ]
    if remaining_state or _source_state_fingerprint(repo) != before_source:
        raise SafetyError("Final Clawpatch state cleanup did not preserve exact project source.")


def _execute_fix(
    repo: Path,
    finding_id: str,
    *,
    inspected: dict[str, Any],
    env: dict[str, str],
    push_mode: str,
    branch: str,
    pushed: bool,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
    require_project_gates: bool = True,
    finalize: bool = False,
) -> tuple[dict[str, Any], bool]:
    if _source_paths(repo):
        raise SafetyError("Pre-existing source changes block the current Clawpatch fix.")
    argv = ["clawpatch", "fix", "--finding", finding_id]
    head_before = _git_text(repo, ["git", "rev-parse", "HEAD"])
    _require_no_process(repo)
    fixed = _fix_command(repo, argv, env=env)
    post_fix_show = _show_finding(repo, finding_id, env=env, required_status="uncertain")
    patch = _patch_attempt_from_show(post_fix_show, str(fixed["patchAttempt"]), finding_id)
    files = [str(path) for path in patch["filesChanged"]]
    _validate_attempt_paths(repo, files)
    try:
        gate_runs = _run_project_gates(
            repo,
            finding_id=finding_id,
            required=require_project_gates,
        )
    except SafetyError as exc:
        raise _UnresolvedFinding(
            str(exc),
            finding_id=finding_id,
            outcome="project-gates-failed",
        ) from exc
    _validate_attempt_paths(repo, files)
    validation = _revalidate(
        repo,
        finding_id,
        env=env,
        expected_paths=files,
        progress=progress,
        current=current,
        total=total,
    )
    revalidation_outcome = str(validation.get("outcome"))
    commit = (
        _commit_attempt(
            repo,
            finding_id,
            files,
            branch=branch,
            outcome=revalidation_outcome,
        )
        if finalize and revalidation_outcome == "fixed"
        else ""
    )
    if finalize and revalidation_outcome != "fixed":
        raise _UnresolvedFinding(
            f"Clawpatch revalidation kept {finding_id} {revalidation_outcome}; partial work "
            "must remain local to the same-finding iteration loop.",
            finding_id=finding_id,
            outcome=revalidation_outcome,
        )
    if finalize and push_mode == "each" and commit:
        _push_and_verify(repo, branch, first=not pushed)
        pushed = True
    return {
        "finding_id": finding_id,
        "inspection": inspected,
        "head_before": head_before,
        "patch_attempt": fixed["patchAttempt"],
        "files_changed": files,
        "gate_runs": gate_runs,
        "revalidation": validation,
        "commit": commit,
    }, pushed


def _resume_stopped_attempt(
    repo: Path,
    checkpoint: dict[str, Any],
    *,
    env: dict[str, str],
    push_mode: str,
    branch: str,
    pushed: bool,
    progress: Callable[[dict[str, Any]], None] | None = None,
    require_project_gates: bool = True,
    advance_uncertain: bool = False,
) -> tuple[dict[str, Any], bool]:
    finding_id = str(checkpoint["finding_id"])
    recorded_owned_paths = sorted(str(path) for path in checkpoint["owned_paths"])
    owned_paths = _normalized_stopped_owned_paths(repo, checkpoint, recorded_owned_paths)
    if not owned_paths or owned_paths != _source_paths(repo):
        raise SafetyError(
            "Stopped Clawpatch progress does not exactly own the current source changes."
        )
    inspected = _show_finding(
        repo,
        finding_id,
        env=env,
        required_status=None,
        progress=progress,
    )
    finding = inspected["finding"]
    finding_status = str(finding.get("status"))
    if finding_status not in {"uncertain", "open", "fixed", "false-positive"}:
        raise SafetyError(
            f"Stopped Clawpatch finding {finding_id} has unsupported status {finding_status!r}."
        )
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    temporary_commit = str(checkpoint.get("temporary_commit", ""))
    candidates = []
    if temporary_commit:
        if current_head != checkpoint.get("head_before"):
            raise SafetyError(
                "Stopped Clawpatch iteration chain is not based on its recorded starting HEAD."
            )
        recorded_fingerprint = str(checkpoint.get("owned_source_fingerprint", ""))
        if (
            not recorded_fingerprint
            or _owned_source_fingerprint(repo, owned_paths) != recorded_fingerprint
        ):
            raise SafetyError(
                "Stopped Clawpatch iteration chain no longer matches its exact source fingerprint."
            )
        iteration_paths = _verify_iteration_commit(
            repo,
            finding_id=finding_id,
            original_head=current_head,
            temporary_commit=temporary_commit,
            require_current=False,
        )
        if not iteration_paths or not set(iteration_paths).issubset(owned_paths):
            raise SafetyError(
                "Stopped Clawpatch iteration commit is not a nonempty subset of its exact "
                "checkpoint-owned paths."
            )
        valid_bases = {current_head, temporary_commit}
        temporary_tree = _git_text(repo, ["git", "rev-parse", f"{temporary_commit}^{{tree}}"])
        for attempt in inspected["patchAttempts"]:
            if not isinstance(attempt, dict) or attempt.get("status") not in {
                "applied",
                "failed",
            }:
                continue
            patch_attempt_id = attempt.get("patchAttemptId")
            finding_ids = attempt.get("findingIds")
            files_changed = attempt.get("filesChanged")
            git_record = attempt.get("git")
            if (
                isinstance(patch_attempt_id, str)
                and patch_attempt_id.strip()
                and isinstance(finding_ids, list)
                and finding_id in finding_ids
                and isinstance(files_changed, list)
                and all(isinstance(path, str) and path for path in files_changed)
                and set(files_changed).issubset(owned_paths)
                and isinstance(git_record, dict)
            ):
                attempt_base = git_record.get("baseSha")
                if not isinstance(attempt_base, str):
                    continue
                if attempt_base not in valid_bases:
                    if not re.fullmatch(r"[0-9a-f]{40}", attempt_base):
                        continue
                    try:
                        attempt_iteration_paths = _verify_iteration_commit(
                            repo,
                            finding_id=finding_id,
                            original_head=current_head,
                            temporary_commit=attempt_base,
                            require_current=False,
                        )
                        attempt_tree = _git_text(
                            repo, ["git", "rev-parse", f"{attempt_base}^{{tree}}"]
                        )
                    except SafetyError:
                        continue
                    if (
                        not attempt_iteration_paths
                        or not set(attempt_iteration_paths).issubset(owned_paths)
                        or attempt_tree != temporary_tree
                    ):
                        continue
                _validate_attempt_paths_syntax(list(files_changed))
                candidates.append(attempt)
        if not candidates:
            raise SafetyError(
                "Stopped Clawpatch iteration chain has no matching applied or validation-failed "
                "patch attempt at its recorded Git boundary."
            )
        patch = candidates[-1]
    else:
        resumable_statuses = {"applied"}
        if checkpoint.get("last_action") == RepairAction.STOP_TRANSIENT.value:
            resumable_statuses.add("failed")
        for attempt in inspected["patchAttempts"]:
            if not isinstance(attempt, dict) or attempt.get("status") not in resumable_statuses:
                continue
            git_record = attempt.get("git")
            if (
                finding_id in attempt.get("findingIds", [])
                and sorted(attempt.get("filesChanged", [])) == owned_paths
                and isinstance(git_record, dict)
                and _attempt_base_preserves_owned_source(
                    repo,
                    attempt_base=git_record.get("baseSha"),
                    current_head=current_head,
                    owned_paths=owned_paths,
                )
            ):
                candidates.append(attempt)
        if len(candidates) != 1:
            raise SafetyError(
                "Stopped Clawpatch progress requires exactly one resumable patch attempt bound "
                "to the current HEAD and owned source paths."
            )
        patch = candidates[0]
    _validate_attempt_paths(repo, owned_paths)
    project_gate_failure = ""
    try:
        gate_runs = _run_project_gates(
            repo,
            finding_id=finding_id,
            required=require_project_gates,
        )
    except GateFailure as exc:
        gate_runs = []
        project_gate_failure = str(exc)

    if project_gate_failure:
        validation = {
            "finding": finding_id,
            "outcome": finding_status,
            "managerooProjectGateFailureContinuation": True,
            "managerooProjectGateFailure": project_gate_failure,
        }
    elif finding_status == "uncertain":
        try:
            validation = _revalidate(
                repo,
                finding_id,
                env=env,
                expected_paths=owned_paths,
                progress=progress,
            )
        except _UnresolvedFinding as exc:
            if exc.outcome == "revalidation-provider-failed":
                validation = {
                    "finding": finding_id,
                    "outcome": "open",
                    "managerooProviderFailureContinuation": True,
                }
            elif exc.outcome == "revalidation-mutated-source":
                reopened = _show_finding(
                    repo,
                    finding_id,
                    env=env,
                    required_status="open",
                    progress=progress,
                )
                validation = {
                    "finding": finding_id,
                    "outcome": str(reopened["finding"]["status"]),
                    "managerooRevalidationProgress": True,
                }
            else:
                raise
    else:
        validation = {
            "finding": finding_id,
            "outcome": finding_status,
            "managerooResumedRecordedOutcome": True,
        }
    outcome = str(validation["outcome"])
    can_advance_uncertain = bool(
        outcome == "uncertain" and advance_uncertain and not project_gate_failure
    )
    commit = ""
    if outcome == "fixed" or can_advance_uncertain:
        commit = _commit_attempt(
            repo,
            finding_id,
            owned_paths,
            branch=branch,
            outcome=outcome,
        )
    if push_mode == "each" and commit:
        _push_and_verify(repo, branch, first=not pushed)
        pushed = True
    record = {
        "finding_id": finding_id,
        "inspection": inspected,
        "head_before": current_head,
        "patch_attempt": patch["patchAttemptId"],
        "files_changed": owned_paths,
        "gate_runs": gate_runs,
        "revalidation": validation,
        "commit": commit,
        "resumed": True,
    }
    if can_advance_uncertain:
        record["deferred_uncertain"] = True
    return record, pushed


def _required_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SafetyError(f"Clawpatch returned a missing or malformed {field!r} value.")
    return value


def _map_repository(
    repo: Path,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    mapped = _json_clawpatch(
        repo,
        ["clawpatch", "map", "--json"],
        env=env,
        progress=progress,
    )
    mapped_features = _required_int(mapped, "features")
    if mapped_features == 0 and mapped.get("source") == "heuristic":
        mapped = _json_clawpatch(
            repo,
            ["clawpatch", "map", "--source", "agent", "--json"],
            env=env,
            progress=progress,
            phase="map-agent",
        )
        _required_int(mapped, "features")
    return mapped


def _review_probe(
    repo: Path,
    *,
    env: dict[str, str],
    review_limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> dict[str, Any]:
    payload = _json_clawpatch(
        repo,
        [
            "clawpatch",
            "review",
            "--limit",
            str(max(review_limit, 1)),
            "--dry-run",
            "--json",
        ],
        env=env,
        progress=progress,
        current=current,
        total=total,
    )
    if payload.get("dryRun") is not True:
        raise SafetyError("Clawpatch review dry-run did not identify itself as a dry-run.")
    pending = _required_int(payload, "wouldReview")
    if pending < 0 or pending > max(review_limit, 1):
        raise SafetyError("Clawpatch review dry-run returned an impossible pending count.")
    return payload


def _review_completion(
    repo: Path,
    *,
    env: dict[str, str],
    review_limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    payload = _review_probe(
        repo,
        env=env,
        review_limit=review_limit,
        progress=progress,
    )
    if _required_int(payload, "wouldReview") != 0:
        raise SafetyError("Clawpatch still has pending or errored features requiring review.")
    return payload


def _review_all_features(
    repo: Path,
    *,
    env: dict[str, str],
    mapped_features: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if mapped_features < 0:
        raise SafetyError("Clawpatch map returned a negative feature count.")
    review_limit = max(mapped_features, 1)
    completion = _review_probe(
        repo,
        env=env,
        review_limit=review_limit,
        progress=progress,
        current=0,
        total=mapped_features,
    )
    pending = _required_int(completion, "wouldReview")
    reviewed_total = 0
    findings_total = 0
    batches: list[dict[str, Any]] = []
    runs: list[str] = []
    reports: list[str] = []
    while pending > 0:
        jobs = _required_int(completion, "jobs")
        if jobs < 1:
            raise SafetyError("Clawpatch review dry-run returned an invalid worker count.")
        batch_limit = min(pending, jobs)
        batch = _json_clawpatch(
            repo,
            ["clawpatch", "review", "--limit", str(batch_limit), "--json"],
            env=env,
            progress=progress,
            current=reviewed_total + 1,
            total=mapped_features,
        )
        reviewed = _required_int(batch, "reviewed")
        findings = _required_int(batch, "findings")
        if reviewed < 1 or reviewed > batch_limit or findings < 0:
            raise SafetyError("Clawpatch review returned impossible batch counts.")
        next_completion = _review_probe(
            repo,
            env=env,
            review_limit=review_limit,
            progress=progress,
            current=reviewed_total + reviewed,
            total=mapped_features,
        )
        remaining = _required_int(next_completion, "wouldReview")
        if remaining >= pending:
            raise SafetyError(
                "Clawpatch review batch did not reduce pending features; stopping instead of "
                "repeating the same review state."
            )
        if pending - remaining != reviewed:
            raise SafetyError(
                "Clawpatch review batch count did not match the pending-feature transition."
            )
        reviewed_total += reviewed
        findings_total += findings
        if reviewed_total > mapped_features:
            raise SafetyError("Clawpatch review exceeded the mapped feature count.")
        run = batch.get("run")
        if isinstance(run, str) and run:
            runs.append(run)
        report_path = batch.get("report")
        if isinstance(report_path, str) and report_path:
            reports.append(report_path)
        batches.append(batch)
        pending = remaining
        completion = next_completion
    review = {
        "reviewed": reviewed_total,
        "findings": findings_total,
        "jobs": max(
            (
                batch["jobs"]
                for batch in batches
                if isinstance(batch.get("jobs"), int) and not isinstance(batch.get("jobs"), bool)
            ),
            default=0,
        ),
        "runs": runs,
        "reports": reports,
        "batches": batches,
        "next": batches[-1].get("next", "clawpatch status") if batches else "clawpatch status",
    }
    return {"review": review, "completion": completion}


def _resolve_uncertain_findings(
    repo: Path,
    *,
    env: dict[str, str],
    uncertain_total: int,
    require_project_gates: bool,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current_offset: int = 0,
) -> tuple[list[dict[str, Any]], list[str]]:
    if (
        not isinstance(uncertain_total, int)
        or isinstance(uncertain_total, bool)
        or uncertain_total < 0
    ):
        raise SafetyError("Clawpatch returned an invalid uncertain-finding count.")
    if _source_paths(repo):
        raise SafetyError("Uncommitted source changes block uncertain-finding recovery.")
    recovered: list[dict[str, Any]] = []
    reopened: list[str] = []
    for index in range(1, uncertain_total + 1):
        displayed = current_offset + index
        display_total = current_offset + uncertain_total
        finding_id, queue = _next_finding(
            repo,
            env=env,
            status="uncertain",
            progress=progress,
            current=displayed,
            total=display_total,
        )
        if finding_id is None:
            raise SafetyError(
                "Clawpatch uncertain report count changed before every finding could be revalidated."
            )
        inspected = _show_finding(
            repo,
            finding_id,
            env=env,
            required_status="uncertain",
            progress=progress,
            current=displayed,
            total=display_total,
        )
        if progress is not None:
            progress(
                {
                    "phase": "uncertain-revalidation",
                    "current": displayed,
                    "total": display_total,
                    "finding_id": finding_id,
                    "command": f"clawpatch revalidate --finding {finding_id} --json",
                    "inspection": inspected,
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
        gate_runs = _run_project_gates(
            repo,
            finding_id=finding_id,
            required=require_project_gates,
        )
        validation = _revalidate(
            repo,
            finding_id,
            env=env,
            expected_paths=[],
            progress=progress,
            current=displayed,
            total=display_total,
        )
        record = {
            "finding_id": finding_id,
            "queue": queue,
            "inspection": inspected,
            "files_changed": [],
            "gate_runs": gate_runs,
            "revalidation": validation,
            "commit": "",
            "recovered_uncertain": True,
        }
        if validation.get("outcome") == "fixed":
            recovered.append(record)
            if progress is not None:
                progress(
                    {
                        "phase": "fixed",
                        "current": displayed,
                        "total": display_total,
                        "finding_id": finding_id,
                        "commit": "",
                        "detail": "uncertain finding revalidated fixed; no source commit required",
                    }
                )
        elif validation.get("outcome") == "open":
            reopened.append(finding_id)
        else:
            raise SafetyError(
                f"Uncertain-finding recovery returned an unsupported outcome for {finding_id}."
            )
        if _source_paths(repo):
            raise SafetyError("Uncertain-finding revalidation unexpectedly changed source files.")
    remaining, _payload = _next_finding(
        repo,
        env=env,
        status="uncertain",
        progress=progress,
        current=current_offset + uncertain_total,
        total=current_offset + uncertain_total,
    )
    if remaining is not None:
        raise SafetyError(
            "Clawpatch uncertain report contained more findings than its reported total."
        )
    return recovered, reopened


def _final_closure(
    repo: Path,
    *,
    env: dict[str, str],
    state_root: Path,
    push_mode: str,
    branch: str,
    pushed: bool,
    publish_clawpatch_state: bool,
    review_limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
    require_project_gates: bool = True,
    require_fresh_review: bool = False,
    resolve_uncertain: bool = True,
) -> dict[str, Any]:
    _require_no_process(repo)
    review_completion = _review_completion(
        repo,
        env=env,
        review_limit=review_limit,
        progress=progress,
    )
    all_validation = _json_clawpatch(
        repo,
        ["clawpatch", "revalidate", "--all", "--status", "open", "--json"],
        env=env,
        progress=progress,
        current=current,
        total=total,
    )
    report = _json_clawpatch(
        repo,
        ["clawpatch", "report", "--status", "open", "--json"],
        env=env,
        progress=progress,
        current=current,
        total=total,
    )
    if report.get("total") != 0 or report.get("items") != []:
        raise SafetyError("Final Clawpatch report is not exactly total=0 and items=[].")
    uncertain_report = _json_clawpatch(
        repo,
        ["clawpatch", "report", "--status", "uncertain", "--json"],
        env=env,
        progress=progress,
        current=current,
        total=total,
    )
    uncertain_total = _required_int(uncertain_report, "total")
    uncertain_items = uncertain_report.get("items")
    if not isinstance(uncertain_items, list) or len(uncertain_items) != uncertain_total:
        raise SafetyError("Final Clawpatch uncertain report has inconsistent items and total.")
    recovered_findings: list[dict[str, Any]] = []
    recovered_continuations: list[dict[str, Any]] = []
    if uncertain_total and resolve_uncertain:
        current_offset = (
            current if isinstance(current, int) and not isinstance(current, bool) else 0
        )
        fixed_uncertain, reopened = _resolve_uncertain_findings(
            repo,
            env=env,
            uncertain_total=uncertain_total,
            require_project_gates=require_project_gates,
            progress=progress,
            current_offset=current_offset,
        )
        recovered_findings.extend(fixed_uncertain)
        recovery_total = current_offset + uncertain_total
        for reopened_index, expected_finding in enumerate(reopened, start=1):
            displayed = current_offset + len(fixed_uncertain) + reopened_index
            finding_id, queue = _next_finding(
                repo,
                env=env,
                status="open",
                progress=progress,
                current=displayed,
                total=recovery_total,
            )
            if finding_id != expected_finding:
                raise SafetyError(
                    "Clawpatch did not return the same finding after uncertain revalidation "
                    f"reopened it; expected {expected_finding!r}, received {finding_id!r}."
                )
            inspected = _show_finding(
                repo,
                finding_id,
                env=env,
                required_status="open",
                progress=progress,
                current=displayed,
                total=recovery_total,
            )
            if progress is not None:
                progress(
                    {
                        "phase": "finding",
                        "current": displayed,
                        "total": recovery_total,
                        "finding_id": finding_id,
                        "command": f"clawpatch show --finding {finding_id}",
                        "inspection": inspected,
                        "detail": "uncertain revalidation reopened finding; resuming normal repair",
                    }
                )
            record, pushed, continuation_count = _process_finding_until_fixed(
                repo,
                finding_id,
                inspected=inspected,
                env=env,
                push_mode=push_mode,
                branch=branch,
                pushed=pushed,
                state_root=state_root,
                progress=progress,
                current=displayed,
                total=recovery_total,
                require_project_gates=require_project_gates,
            )
            record["queue"] = queue
            record["recovered_uncertain"] = True
            record["continuation_attempts"] = continuation_count
            recovered_findings.append(record)
            recovered_continuations.extend(
                {
                    "finding_id": finding_id,
                    "iteration": iteration,
                    "temporary_local_commit": True,
                }
                for iteration in range(1, continuation_count + 1)
            )
            if progress is not None:
                progress(
                    {
                        "phase": "fixed",
                        "current": displayed,
                        "total": recovery_total,
                        "finding_id": finding_id,
                        "commit": record.get("commit", ""),
                    }
                )
        remaining_open, _payload = _next_finding(
            repo,
            env=env,
            status="open",
            progress=progress,
            current=recovery_total,
            total=recovery_total,
        )
        if remaining_open is not None:
            raise SafetyError(
                "Uncertain-finding recovery produced an unexpected additional open finding."
            )
        all_validation = _json_clawpatch(
            repo,
            ["clawpatch", "revalidate", "--all", "--status", "open", "--json"],
            env=env,
            progress=progress,
            current=recovery_total,
            total=recovery_total,
        )
        report = _json_clawpatch(
            repo,
            ["clawpatch", "report", "--status", "open", "--json"],
            env=env,
            progress=progress,
            current=recovery_total,
            total=recovery_total,
        )
        if report.get("total") != 0 or report.get("items") != []:
            raise SafetyError("Recovered Clawpatch open report is not exactly empty.")
        uncertain_report = _json_clawpatch(
            repo,
            ["clawpatch", "report", "--status", "uncertain", "--json"],
            env=env,
            progress=progress,
            current=recovery_total,
            total=recovery_total,
        )
        if uncertain_report.get("total") != 0 or uncertain_report.get("items") != []:
            raise SafetyError("Final Clawpatch report still contains uncertain findings.")
    status = _json_clawpatch(
        repo,
        ["clawpatch", "status", "--json"],
        env=env,
        progress=progress,
        current=current,
        total=total,
    )
    for field in ("openFindings", "activeLocks", "lockFiles"):
        if _required_int(status, field) != 0:
            raise SafetyError(f"Final Clawpatch status requires {field}=0.")
    final_gates = _run_project_gates(
        repo,
        finding_id="N/A",
        required=require_project_gates,
    )
    if _source_paths(repo):
        raise SafetyError(f"Final closure found uncommitted source changes: {_source_paths(repo)}")
    retains_uncertain_without_resolution = bool(uncertain_total and not resolve_uncertain)
    needs_fresh_review = (
        require_fresh_review and not retains_uncertain_without_resolution
    ) or bool(recovered_findings)
    state_commit = ""
    if not needs_fresh_review:
        state_paths = [
            path
            for path in _status_paths(repo)
            if path == ".clawpatch" or path.startswith(".clawpatch/")
        ]
        if state_paths and publish_clawpatch_state:
            if push_mode == "none":
                raise SafetyError(
                    "Publishing final Clawpatch state requires explicit --push each or --push "
                    "final authorization."
                )
            state_commit = _publish_final_state(repo, branch=branch)
        elif (repo / ".clawpatch").exists() or state_paths:
            if progress is not None:
                progress(
                    {
                        "phase": "state-retained",
                        "current": current,
                        "total": total,
                        "command": "keep .clawpatch for status and queue proof",
                        "attempt": 1,
                        "max_attempts": 1,
                    }
                )
        if push_mode == "final" or state_commit:
            _push_and_verify(repo, branch, first=not pushed)
            pushed = True
        unexpected_paths = [
            path
            for path in _status_paths(repo)
            if path != ".clawpatch" and not path.startswith(".clawpatch/")
        ]
        if unexpected_paths:
            raise SafetyError(
                "Final authorized Git source is not clean after retaining Clawpatch state: "
                + ", ".join(unexpected_paths)
            )
    _require_no_process(repo)
    return {
        "all_revalidation": all_validation,
        "review_completion": review_completion,
        "report": report,
        "uncertain_report": uncertain_report,
        "status": status,
        "gate_runs": final_gates,
        "pushed": pushed,
        "state_commit": state_commit,
        "state_retained": (repo / ".clawpatch").is_dir(),
        "recovered_findings": recovered_findings,
        "recovered_continuations": recovered_continuations,
        "needs_fresh_review": needs_fresh_review,
    }


def release_sweep(
    repo: Path,
    *,
    apply: bool = False,
    branch: str = "auto",
    push_mode: str = "none",
    publish_clawpatch_state: bool = False,
    trusted_host_codex_sandbox_bypass: bool = False,
    fresh: bool = False,
    child_timeout_seconds: int = CLAWPATCH_CHILD_WATCHDOG_SECONDS,
    progress: Callable[[dict[str, Any]], None] | None = None,
    integration_mode: str = "manageroo",
    child_env_overrides: dict[str, str] | None = None,
    advance_uncertain: bool = False,
) -> dict[str, Any]:
    """Automate Clawpatch's documented one-finding workflow without automatic triage."""
    root = _git_root(repo)
    if not apply:
        return _release_sweep_locked(
            root,
            apply=False,
            branch=branch,
            push_mode=push_mode,
            publish_clawpatch_state=publish_clawpatch_state,
            trusted_host_codex_sandbox_bypass=trusted_host_codex_sandbox_bypass,
            fresh=fresh,
            child_timeout_seconds=child_timeout_seconds,
            progress=progress,
            integration_mode=integration_mode,
            child_env_overrides=child_env_overrides,
            advance_uncertain=advance_uncertain,
        )
    with _release_sweep_lock(root):
        return _release_sweep_locked(
            root,
            apply=True,
            branch=branch,
            push_mode=push_mode,
            publish_clawpatch_state=publish_clawpatch_state,
            trusted_host_codex_sandbox_bypass=trusted_host_codex_sandbox_bypass,
            fresh=fresh,
            child_timeout_seconds=child_timeout_seconds,
            progress=progress,
            integration_mode=integration_mode,
            child_env_overrides=child_env_overrides,
            advance_uncertain=advance_uncertain,
        )


def _release_sweep_locked(
    repo: Path,
    *,
    apply: bool = False,
    branch: str = "auto",
    push_mode: str = "none",
    publish_clawpatch_state: bool = False,
    trusted_host_codex_sandbox_bypass: bool = False,
    fresh: bool = False,
    child_timeout_seconds: int = CLAWPATCH_CHILD_WATCHDOG_SECONDS,
    progress: Callable[[dict[str, Any]], None] | None = None,
    integration_mode: str = "manageroo",
    child_env_overrides: dict[str, str] | None = None,
    advance_uncertain: bool = False,
    _fixed_point_generation: int = 1,
    _fixed_point_seen_trees: tuple[str, ...] = (),
    _prior_results: tuple[dict[str, Any], ...] = (),
    _prior_continuations: tuple[dict[str, Any], ...] = (),
    _prior_false_positives: tuple[dict[str, Any], ...] = (),
    _prior_review_generations: tuple[dict[str, Any], ...] = (),
    _already_pushed: bool = False,
) -> dict[str, Any]:
    root = _git_root(repo)
    if integration_mode not in {"manageroo", "external"}:
        raise SafetyError("integration_mode must be one of: manageroo, external.")
    require_project_gates = integration_mode == "manageroo"
    state_root = _release_state_root(root, integration_mode=integration_mode)
    if progress is not None:
        progress(
            {
                "phase": "preflight",
                "current": "?",
                "total": "?",
                "command": "clawpatch --version",
                "attempt": 1,
                "max_attempts": 1,
            }
        )
    version = _clawpatch_version(root)
    current_branch = _git_text(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    head_before = _git_text(root, ["git", "rev-parse", "HEAD"])
    if push_mode not in {"none", "each", "final"}:
        raise SafetyError("push_mode must be one of: none, each, final.")
    report: dict[str, Any] = {
        "ok": True,
        "apply": apply,
        "repo": str(root),
        "branch": current_branch,
        "git_head_before": head_before,
        "clawpatch_version": version,
        "lifecycle": LIFECYCLE,
        "push_mode": push_mode,
        "integration_mode": integration_mode,
        "publish_clawpatch_state": publish_clawpatch_state,
        "advance_uncertain": advance_uncertain,
        "results": list(_prior_results),
        "continuations": list(_prior_continuations),
        "false_positives": list(_prior_false_positives),
        "review_generations": list(_prior_review_generations),
    }
    generation_result_start = len(report["results"])
    if not apply:
        report["planned_branch"] = branch
        return report

    _require_no_process(root)
    env = _release_clawpatch_env(
        trusted_host_codex_sandbox_bypass=trusted_host_codex_sandbox_bypass,
        allow_sandbox_bypass_fallback=(integration_mode == "external"),
        child_timeout_seconds=child_timeout_seconds,
        child_env_overrides=child_env_overrides,
    )
    if integration_mode == "external":
        _migrate_legacy_external_progress(root, state_root=state_root)
    if fresh:
        _prepare_fresh_release(
            root,
            env=env,
            progress=progress,
            state_root=state_root,
        )
    durable_progress = _load_release_progress(root, state_root=state_root)
    preexisting_source = _source_paths(root)
    selected_branch = current_branch
    pushed = _already_pushed
    resumed_checkpoint = False
    expected_unapplied_finding: str | None = None
    resumed_checkpoint_kind = "stopped applied attempt"
    if durable_progress is not None:
        if durable_progress["branch"] != current_branch:
            raise SafetyError(
                "Interrupted Clawpatch release progress is bound to branch "
                f"{durable_progress['branch']!r}, not {current_branch!r}."
            )
        if _rebuilt_generation_supersedes_empty_checkpoint(root, durable_progress):
            reset_finding = str(durable_progress["finding_id"])
            if progress is not None:
                progress(
                    {
                        "phase": "reset-recovery",
                        "current": "?",
                        "total": "?",
                        "finding_id": reset_finding,
                        "command": "retire source-clean checkpoint from prior ClawPatch generation",
                        "attempt": 1,
                        "max_attempts": 1,
                        "owned_paths": [],
                    }
                )
            _clear_release_progress(root, state_root=state_root)
            report["reset_recovery"] = {
                "finding_id": reset_finding,
                "owned_paths": [],
                "generation": "rebuilt",
            }
            durable_progress = None
        if durable_progress is not None:
            interrupted_phase = str(durable_progress["phase"])
            recovered_progress = _recover_interrupted_source_clean_fix(
                root,
                durable_progress,
                state_root=state_root,
            )
            if recovered_progress is not None:
                durable_progress = recovered_progress
                report["interrupted_phase_recovery"] = {
                    "finding_id": str(durable_progress["finding_id"]),
                    "prior_phase": interrupted_phase,
                    "owned_paths": [],
                }
                if progress is not None:
                    progress(
                        {
                            "phase": "resume",
                            "current": 1,
                            "total": "?",
                            "finding_id": str(durable_progress["finding_id"]),
                            "detail": (
                                "interrupted source-clean fix had no repair content; "
                                "resuming the same finding"
                            ),
                            "owned_paths": [],
                        }
                    )
        if (
            durable_progress is not None
            and preexisting_source
            and durable_progress["head_before"] != head_before
            and durable_progress.get("owned_source_fingerprint")
            and not _checkpoint_proves_exact_source(
                root,
                durable_progress,
                preexisting_source,
            )
        ):
            checkpoint_inspection = _show_finding(
                root,
                str(durable_progress["finding_id"]),
                env=env,
                required_status=None,
                progress=progress,
                current=1,
                total="?",
            )
            later_applied = _checkpoint_later_applied_attempt(
                root,
                durable_progress,
                inspected=checkpoint_inspection,
            )
            if later_applied is not None:
                recovered_paths = list(later_applied["owned_paths"])
                recovered_attempt = later_applied["patch_attempt"]
                recovered_status = str(recovered_attempt.get("status", ""))
                recovered_action = (
                    RepairAction.STOP_TRANSIENT
                    if recovered_status == "failed"
                    else str(durable_progress.get("last_action", ""))
                )
                durable_progress = _write_release_progress(
                    root,
                    finding_id=str(durable_progress["finding_id"]),
                    branch=str(durable_progress["branch"]),
                    head_before=head_before,
                    phase="stopped",
                    owned_paths=recovered_paths,
                    last_action=recovered_action,
                    state_root=state_root,
                )
                report["recovered_later_applied_attempt"] = {
                    "finding_id": str(later_applied["finding_id"]),
                    "patch_attempt": str(recovered_attempt["patchAttemptId"]),
                    "owned_paths": recovered_paths,
                }
                if progress is not None:
                    progress(
                        {
                            "phase": "resume",
                            "current": 1,
                            "total": "?",
                            "finding_id": str(later_applied["finding_id"]),
                            "detail": (
                                "recognized the later ClawPatch repair at the current HEAD; "
                                "resuming its exact source paths"
                            ),
                            "owned_paths": recovered_paths,
                        }
                    )
        if (
            durable_progress is not None
            and preexisting_source
            and durable_progress["head_before"] != head_before
            and durable_progress.get("owned_source_fingerprint")
            and not _checkpoint_proves_exact_source(
                root,
                durable_progress,
                preexisting_source,
            )
        ):
            ambiguous_recovery = _preserve_ambiguous_checkpoint_source(
                root,
                durable_progress,
                preexisting_source,
                state_root=state_root,
            )
            if ambiguous_recovery is not None:
                report["ambiguous_checkpoint_recovery"] = ambiguous_recovery
                if progress is not None:
                    progress(
                        {
                            "phase": "reset-recovery",
                            "current": "?",
                            "total": "?",
                            "finding_id": ambiguous_recovery["finding_id"],
                            "command": (
                                "preserve ambiguous checkpoint source in a local recovery ref; "
                                "restore current HEAD; continue ClawPatch queue"
                            ),
                            "attempt": 1,
                            "max_attempts": 1,
                            "owned_paths": list(ambiguous_recovery["paths"]),
                            "preserved_ref": ambiguous_recovery["preserved_ref"],
                            "receipt": ambiguous_recovery["receipt"],
                        }
                    )
                durable_progress = None
                preexisting_source = _source_paths(root)
        if durable_progress is not None and durable_progress["head_before"] != head_before:
            if _checkpoint_can_follow_supervisor_upgrade(root, durable_progress):
                if preexisting_source and not _checkpoint_proves_exact_source(
                    root,
                    durable_progress,
                    preexisting_source,
                ):
                    raise SafetyError(
                        "Interrupted Clawpatch release progress cannot prove exact "
                        "checkpoint-owned source content; preserving ambiguous changes for "
                        "operator review: " + ", ".join(preexisting_source)
                    )
                durable_progress = _write_release_progress(
                    root,
                    finding_id=str(durable_progress["finding_id"]),
                    branch=str(durable_progress["branch"]),
                    head_before=head_before,
                    phase=str(durable_progress["phase"]),
                    owned_paths=list(durable_progress["owned_paths"]),
                    last_action=str(durable_progress.get("last_action", "")),
                    state_root=state_root,
                )
            else:
                if preexisting_source:
                    if preexisting_source != sorted(durable_progress["owned_paths"]):
                        raise SafetyError(
                            "Interrupted Clawpatch release progress no longer owns the exact "
                            "current source paths."
                        )
                    if not _checkpoint_proves_exact_source(
                        root,
                        durable_progress,
                        preexisting_source,
                    ):
                        raise SafetyError(
                            "Interrupted Clawpatch release progress cannot prove exact "
                            "checkpoint-owned source content; preserving ambiguous changes for "
                            "operator review: " + ", ".join(preexisting_source)
                        )
                    durable_progress = _write_release_progress(
                        root,
                        finding_id=str(durable_progress["finding_id"]),
                        branch=str(durable_progress["branch"]),
                        head_before=head_before,
                        phase=str(durable_progress["phase"]),
                        owned_paths=list(durable_progress["owned_paths"]),
                        last_action=str(durable_progress.get("last_action", "")),
                        state_root=state_root,
                    )
                else:
                    if _clean_descendant_retires_verified_checkpoint(root, durable_progress):
                        reset_finding = str(durable_progress["finding_id"])
                        if progress is not None:
                            progress(
                                {
                                    "phase": "reset-recovery",
                                    "current": "?",
                                    "total": "?",
                                    "finding_id": reset_finding,
                                    "command": (
                                        "retire verified stale recovery wrapper; "
                                        "preserve ClawPatch queue"
                                    ),
                                    "attempt": 1,
                                    "max_attempts": 1,
                                    "owned_paths": [],
                                }
                            )
                        _clear_release_progress(root, state_root=state_root)
                        report["reset_recovery"] = {
                            "finding_id": reset_finding,
                            "owned_paths": [],
                            "generation": "clean-descendant",
                        }
                        durable_progress = None
                    elif not _checkpoint_completed_commit(root, durable_progress):
                        raise SafetyError(
                            "Interrupted Clawpatch release progress no longer matches the "
                            "current Git HEAD."
                        )
                    else:
                        _clear_release_progress(root, state_root=state_root)
                        durable_progress = None
    if durable_progress is not None and _rebuilt_generation_owns_checkpoint_source(
        root, durable_progress
    ):
        reset_finding = str(durable_progress["finding_id"])
        reset_paths = list(durable_progress["owned_paths"])
        if progress is not None:
            progress(
                {
                    "phase": "reset-recovery",
                    "current": "?",
                    "total": "?",
                    "finding_id": reset_finding,
                    "command": "restore exact fingerprinted interrupted source",
                    "attempt": 1,
                    "max_attempts": 1,
                    "owned_paths": reset_paths,
                }
            )
        _discard_checkpoint_owned_source(root, reset_paths)
        _clear_release_progress(root, state_root=state_root)
        report["reset_recovery"] = {
            "finding_id": reset_finding,
            "owned_paths": reset_paths,
            "generation": "rebuilt",
        }
        durable_progress = None
        preexisting_source = _source_paths(root)
    if preexisting_source and durable_progress is None:
        raise SafetyError(
            "Clawpatch release sweep found pre-existing source changes: "
            + ", ".join(preexisting_source)
        )
    if durable_progress is not None and branch not in {"auto", "current", current_branch}:
        raise SafetyError(
            "Cannot create a different branch while resuming interrupted Clawpatch release progress."
        )
    if push_mode != "none":
        _require_synchronized_remote_branch(root, current_branch, progress=progress)
    if durable_progress is not None:
        if durable_progress["phase"] != "stopped":
            raise SafetyError(
                "Only a stopped Clawpatch checkpoint can resume an existing applied attempt."
            )
        if durable_progress["owned_paths"] == []:
            checkpoint_inspection = _show_finding(
                root,
                str(durable_progress["finding_id"]),
                env=env,
                required_status=None,
                progress=progress,
                current=1,
                total="?",
            )
            later_applied = _checkpoint_later_applied_attempt(
                root,
                durable_progress,
                inspected=checkpoint_inspection,
            )
            if later_applied is not None:
                recovered_paths = list(later_applied["owned_paths"])
                durable_progress = _write_release_progress(
                    root,
                    finding_id=str(durable_progress["finding_id"]),
                    branch=str(durable_progress["branch"]),
                    head_before=str(durable_progress["head_before"]),
                    phase="stopped",
                    owned_paths=recovered_paths,
                    last_action=str(durable_progress.get("last_action", "")),
                    state_root=state_root,
                )
                report["recovered_later_applied_attempt"] = {
                    "finding_id": str(later_applied["finding_id"]),
                    "patch_attempt": str(later_applied["patch_attempt"]["patchAttemptId"]),
                    "owned_paths": recovered_paths,
                }
                if progress is not None:
                    progress(
                        {
                            "phase": "resume",
                            "current": 1,
                            "total": "?",
                            "finding_id": str(later_applied["finding_id"]),
                            "detail": (
                                "recognized the later applied repair at the stopped checkpoint "
                                "HEAD; resuming its exact source paths"
                            ),
                            "owned_paths": recovered_paths,
                        }
                    )
        if durable_progress["owned_paths"] == []:
            false_positive_without_source = _checkpoint_false_positive_without_source(
                root,
                durable_progress,
                env=env,
                progress=progress,
                inspected=checkpoint_inspection,
            )
            if false_positive_without_source is not None:
                finding_id = str(false_positive_without_source["finding_id"])
                report["false_positives"].append(
                    {
                        "finding_id": finding_id,
                        "inspection": false_positive_without_source["inspection"],
                        "head_before": false_positive_without_source["head_before"],
                        "temporary_commit": false_positive_without_source["temporary_commit"],
                        "discarded_paths": list(false_positive_without_source["discarded_paths"]),
                        "patch_attempts": list(false_positive_without_source["patch_attempts"]),
                        "resumed": True,
                    }
                )
                _clear_release_progress(root, state_root=state_root)
                durable_progress = None
                resumed_checkpoint = True
                resumed_checkpoint_kind = "false-positive reverted iteration"
                if progress is not None:
                    progress(
                        {
                            "phase": "false-positive",
                            "current": 1,
                            "total": "?",
                            "finding_id": finding_id,
                            "commit": "",
                            "detail": (
                                "resumed false-positive checkpoint already returned exactly "
                                "to its original source tree"
                            ),
                            "resumed": True,
                        }
                    )
            else:
                fixed_without_source = _checkpoint_fixed_without_source(
                    root,
                    durable_progress,
                    env=env,
                    progress=progress,
                    inspected=checkpoint_inspection,
                )
            if false_positive_without_source is not None:
                pass
            elif fixed_without_source is not None:
                finding_id = str(fixed_without_source["finding_id"])
                record = {
                    "finding_id": finding_id,
                    "inspection": fixed_without_source["inspection"],
                    "head_before": fixed_without_source["head_before"],
                    "patch_attempt": fixed_without_source["patch_attempt"],
                    "patch_attempts": list(fixed_without_source["patch_attempts"]),
                    "files_changed": [],
                    "gate_runs": [],
                    "revalidation": {
                        "finding": finding_id,
                        "outcome": "fixed",
                        "managerooResumedRecordedOutcome": True,
                    },
                    "commit": "",
                    "resumed": True,
                    "no_source_commit": True,
                }
                report["results"].append(record)
                _clear_release_progress(root, state_root=state_root)
                durable_progress = None
                resumed_checkpoint = True
                resumed_checkpoint_kind = "fixed no-source attempt"
                if progress is not None:
                    progress(
                        {
                            "phase": "fixed",
                            "current": 1,
                            "total": "?",
                            "finding_id": finding_id,
                            "commit": "",
                            "detail": "fixed source-clean checkpoint; no source commit required",
                            "resumed": True,
                        }
                    )
            else:
                unapplied = _checkpoint_unapplied_attempt(
                    root,
                    durable_progress,
                    env=env,
                    progress=progress,
                    inspected=checkpoint_inspection,
                )
                if unapplied is None:
                    inspected_finding = checkpoint_inspection.get("finding")
                    inspected_attempts = checkpoint_inspection.get("patchAttempts")
                    checkpoint_finding_id = str(durable_progress["finding_id"])
                    if (
                        not isinstance(inspected_finding, dict)
                        or inspected_finding.get("id") != checkpoint_finding_id
                        or inspected_finding.get("status") != "open"
                        or not isinstance(inspected_attempts, list)
                    ):
                        raise SafetyError(
                            "Stopped Clawpatch progress has no source changes and no matching "
                            "open finding at the current HEAD."
                        )
                    unapplied = {
                        "finding_id": checkpoint_finding_id,
                        "patch_attempts": [
                            str(attempt["patchAttemptId"])
                            for attempt in inspected_attempts
                            if isinstance(attempt, dict)
                            and isinstance(attempt.get("patchAttemptId"), str)
                            and attempt["patchAttemptId"]
                        ],
                        "inspection": checkpoint_inspection,
                    }
                expected_unapplied_finding = str(unapplied["finding_id"])
                report["interrupted_unapplied_attempt"] = {
                    "finding_id": expected_unapplied_finding,
                    "patch_attempts": list(unapplied["patch_attempts"]),
                }
                _clear_release_progress(root, state_root=state_root)
                durable_progress = None
                resumed_checkpoint = True
                resumed_checkpoint_kind = "stopped planned attempt"
                if progress is not None:
                    progress(
                        {
                            "phase": "resume",
                            "current": 1,
                            "total": "?",
                            "finding_id": expected_unapplied_finding,
                            "detail": "source-clean planned attempt interrupted; resuming same finding",
                        }
                    )
        else:
            try:
                resumed, pushed = _resume_stopped_attempt(
                    root,
                    durable_progress,
                    env=env,
                    push_mode=push_mode,
                    branch=current_branch,
                    pushed=pushed,
                    progress=progress,
                    require_project_gates=require_project_gates,
                    advance_uncertain=advance_uncertain,
                )
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                raise SafetyError(
                    "Clawpatch could not safely resume the stopped applied attempt; the checkpoint "
                    f"and exact source changes remain in place.\n{exc}"
                ) from exc
            resumed_checkpoint = True
            resumed_outcome = resumed.get("revalidation", {}).get("outcome")
            resumed_uncertain_can_advance = bool(
                resumed_outcome == "uncertain" and resumed.get("deferred_uncertain")
            )
            if resumed_outcome == "open" or (
                resumed_outcome == "uncertain" and not resumed_uncertain_can_advance
            ):
                finding_id = str(resumed["finding_id"])
                provider_failure_continuation = bool(
                    resumed.get("revalidation", {}).get("managerooProviderFailureContinuation")
                )
                uncertain_evidence_continuation = resumed_outcome == "uncertain"
                original_head = str(resumed["head_before"])
                seen_states = {_git_text(root, ["git", "rev-parse", f"{original_head}^{{tree}}"])}
                temporary_commit = ""
                try:
                    temporary_commit, _owned_paths, _state = _save_partial_iteration(
                        root,
                        finding_id=finding_id,
                        branch=current_branch,
                        original_head=original_head,
                        temporary_commit="",
                        seen_states=seen_states,
                        state_root=state_root,
                    )
                    inspected = _show_finding(
                        root,
                        finding_id,
                        env=env,
                        required_status=(
                            None
                            if provider_failure_continuation or uncertain_evidence_continuation
                            else "open"
                        ),
                        progress=progress,
                        current=1,
                        total="?",
                    )
                    if (
                        provider_failure_continuation or uncertain_evidence_continuation
                    ) and inspected["finding"].get("status") not in {"open", "uncertain"}:
                        raise SafetyError(
                            "ClawPatch revalidation evidence can continue only the same open "
                            "or uncertain finding."
                        )
                    completed, pushed, additional_continuations = _process_finding_until_fixed(
                        root,
                        finding_id,
                        inspected=inspected,
                        env=env,
                        push_mode=push_mode,
                        branch=current_branch,
                        pushed=pushed,
                        state_root=state_root,
                        progress=progress,
                        current=1,
                        total="?",
                        require_project_gates=require_project_gates,
                        resume_original_head=original_head,
                        resume_temporary_commit=temporary_commit,
                        resume_seen_states=seen_states,
                        resume_attempt=2,
                        resume_continuations=1,
                        advance_uncertain=advance_uncertain,
                    )
                except BaseException:
                    current_resume_head = _git_text(root, ["git", "rev-parse", "HEAD"])
                    if (temporary_commit and current_resume_head == temporary_commit) or (
                        not temporary_commit and current_resume_head == original_head
                    ):
                        _stop_finding_iteration(
                            root,
                            finding_id=finding_id,
                            branch=current_branch,
                            original_head=original_head,
                            temporary_commit=temporary_commit,
                            seen_states=seen_states,
                            state_root=state_root,
                        )
                    raise
                completed["resumed"] = True
                report["results"].append(completed)
                report["continuations"].extend(
                    {
                        "finding_id": finding_id,
                        "iteration": iteration,
                        "temporary_local_commit": True,
                        "resumed": True,
                    }
                    for iteration in range(1, additional_continuations + 1)
                )
                resumed = completed
                resumed_phase = "fixed"
                resumed_detail = (
                    f"stopped attempt revalidated {resumed_outcome}, continued locally, then fixed"
                )
            elif resumed_uncertain_can_advance:
                finding_id = str(resumed["finding_id"])
                _clear_release_progress(root, state_root=state_root)
                report["results"].append(resumed)
                resumed_phase = "uncertain"
                resumed_detail = "stopped attempt committed as uncertain; continuing queue"
            elif resumed_outcome == "false-positive":
                finding_id = str(resumed["finding_id"])
                discarded_paths = sorted(str(path) for path in resumed["files_changed"])
                if discarded_paths != _source_paths(root):
                    raise SafetyError(
                        "Resumed false-positive source no longer matches its exact "
                        "checkpoint-owned paths."
                    )
                _discard_checkpoint_owned_source(root, discarded_paths)
                _clear_release_progress(root, state_root=state_root)
                report["false_positives"].append(
                    {
                        "finding_id": finding_id,
                        "inspection": resumed["inspection"],
                        "head_before": resumed["head_before"],
                        "temporary_commit": str(durable_progress.get("temporary_commit", "")),
                        "discarded_paths": discarded_paths,
                        "patch_attempts": [resumed["patch_attempt"]],
                        "resumed": True,
                    }
                )
                resumed["files_changed"] = []
                resumed["false_positive"] = True
                resumed_phase = "false-positive"
                resumed_detail = (
                    "stopped attempt is false-positive; restored exact owned source and "
                    "continued queue"
                )
            elif resumed_outcome == "fixed":
                _clear_release_progress(root, state_root=state_root)
                report["results"].append(resumed)
                resumed_phase = "fixed"
                resumed_detail = "stopped attempt revalidated fixed; continuing queue"
            else:
                raise SafetyError(
                    "Resumed Clawpatch attempt returned an unsupported outcome after validation."
                )
            if progress is not None:
                progress(
                    {
                        "phase": resumed_phase,
                        "current": 1,
                        "total": "?",
                        "finding_id": resumed["finding_id"],
                        "commit": resumed.get("commit", ""),
                        "detail": resumed_detail,
                        "resumed": True,
                    }
                )
            durable_progress = None
            preexisting_source = _source_paths(root)
            if preexisting_source:
                raise SafetyError(
                    "Resumed Clawpatch attempt did not leave a source-clean continuation point: "
                    + ", ".join(preexisting_source)
                )
    if (
        integration_mode == "external"
        and not fresh
        and not (root / ".clawpatch" / "project.json").is_file()
    ):
        if progress is not None:
            progress(
                {
                    "phase": "init",
                    "current": "?",
                    "total": "?",
                    "command": "clawpatch init --json",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
        initialized = _json_clawpatch(
            root,
            ["clawpatch", "init", "--json"],
            env=env,
            progress=None,
        )
        if initialized.get("created") is not True or initialized.get("next") != "clawpatch map":
            raise SafetyError("Clawpatch initialization returned an unexpected state transition.")
        if _source_paths(root):
            raise SafetyError("Clawpatch initialization unexpectedly changed project source files.")
        report["init"] = initialized
    if (
        not resumed_checkpoint
        and durable_progress is None
        and branch == "auto"
        and current_branch in {"main", "master", "HEAD"}
    ):
        selected_branch = "clawpatch/release-sweep-" + datetime.now(timezone.utc).strftime(
            "%Y%m%d-%H%M%S"
        )
        _must_run(["git", "switch", "-c", selected_branch], cwd=root, timeout=120)
    elif not resumed_checkpoint and durable_progress is None and branch not in {"auto", "current"}:
        selected_branch = branch
        _must_run(["git", "switch", "-c", selected_branch], cwd=root, timeout=120)
    elif branch == "current" and current_branch == "HEAD":
        raise SafetyError("--branch current cannot be used from a detached HEAD.")
    status = _json_clawpatch(
        root,
        ["clawpatch", "status", "--json"],
        env=env,
        progress=progress,
    )
    if _required_int(status, "activeLocks") or _required_int(status, "lockFiles"):
        _require_no_process(root)
        _json_clawpatch(
            root,
            ["clawpatch", "clean-locks", "--stale-only", "--json"],
            env=env,
            progress=progress,
        )

    if not resumed_checkpoint:
        if progress is not None:
            gate_command = (
                "configured repository gates"
                if require_project_gates or (root / PROJECT_DIR / "config.toml").is_file()
                else "ClawPatch-owned validation (no Manageroo gates configured)"
            )
            progress(
                {
                    "phase": "baseline-validation",
                    "current": "?",
                    "total": "?",
                    "command": gate_command,
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
        _run_project_gates(
            root,
            finding_id="baseline-preflight",
            required=require_project_gates,
        )
        baseline_changes = _source_paths(root)
        if baseline_changes:
            raise SafetyError(
                "Clawpatch baseline validation changed project source files: "
                + ", ".join(baseline_changes)
            )

    if resumed_checkpoint:
        mapped = {"resumed": True, "features": 0}
        mapped_features = 0
        review = {
            "resumed": True,
            "review": {"reviewed": 0, "findings": 0},
            "completion": {"skipped": f"resumed {resumed_checkpoint_kind}"},
        }
    else:
        mapped = _map_repository(root, env=env, progress=progress)
        mapped_features = _required_int(mapped, "features")
        review = _review_all_features(
            root,
            env=env,
            mapped_features=mapped_features,
            progress=progress,
        )
    report["map"] = mapped
    report["review"] = review
    open_findings = status.get("openFindings")
    reviewed_findings = review.get("review", {}).get("findings", 0)
    total_findings = (
        open_findings + reviewed_findings
        if isinstance(open_findings, int) and not isinstance(open_findings, bool)
        else reviewed_findings
    )
    if (
        not isinstance(total_findings, int)
        or isinstance(total_findings, bool)
        or total_findings < 0
    ):
        raise SafetyError(
            "Clawpatch returned an invalid open-finding count for progress reporting."
        )
    total_findings += len(report["results"])
    current_finding = len(report["results"])
    while True:
        displayed_finding = current_finding + 1
        finding_id, queue = _next_finding(
            root,
            env=env,
            progress=progress,
            current=displayed_finding,
            total=total_findings,
        )
        if expected_unapplied_finding is not None:
            if finding_id != expected_unapplied_finding:
                raise SafetyError(
                    "Clawpatch did not return the interrupted source-clean finding; expected "
                    f"{expected_unapplied_finding!r}, received {finding_id!r}."
                )
            expected_unapplied_finding = None
        if finding_id is None:
            break
        try:
            inspected = _show_finding(
                root,
                finding_id,
                env=env,
                required_status="open",
                progress=progress,
                current=displayed_finding,
                total=total_findings,
            )
        except _MissingFinding as exc:
            raise SafetyError(
                f"Clawpatch selected missing finding {finding_id}. The supervisor stopped without "
                "remapping, reviewing, triaging, skipping, or changing the queue."
            ) from exc
        if progress is not None:
            progress(
                {
                    "phase": "finding",
                    "current": displayed_finding,
                    "total": total_findings,
                    "finding_id": finding_id,
                    "command": f"clawpatch show --finding {finding_id}",
                    "inspection": inspected,
                }
            )
        try:
            record, pushed, continuation_count = _process_finding_until_fixed(
                root,
                finding_id,
                inspected=inspected,
                env=env,
                push_mode=push_mode,
                branch=selected_branch,
                pushed=pushed,
                progress=progress,
                current=displayed_finding,
                total=total_findings,
                require_project_gates=require_project_gates,
                state_root=state_root,
                advance_uncertain=advance_uncertain,
            )
        except BaseException as exc:
            stopped = _load_release_progress(root, state_root=state_root)
            owned_paths = (
                list(stopped["owned_paths"])
                if stopped is not None and stopped.get("finding_id") == finding_id
                else _source_paths(root)
            )
            outcome = exc.outcome if isinstance(exc, _UnresolvedFinding) else "command-failed"
            repair_action = (
                exc.repair_action
                if isinstance(exc, _UnresolvedFinding) and exc.repair_action is not None
                else RepairAction.STOP_TERMINAL
            )
            if progress is not None:
                progress(
                    {
                        "phase": "stopped",
                        "current": displayed_finding,
                        "total": total_findings,
                        "finding_id": finding_id,
                        "outcome": outcome,
                        "owned_paths": owned_paths,
                        "detail": "finding stopped safely; source remains in place",
                    }
                )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ClawpatchStop(
                f"Clawpatch stopped on {outcome!r} for {finding_id}. The supervisor left the "
                f"source changes in place at exactly {owned_paths}, did not stash or triage "
                "anything, did not advance the queue, and stopped after source progress ended.\n"
                f"{exc}",
                repair_action=repair_action,
            ) from exc
        record["queue"] = queue
        record["continuation_attempts"] = continuation_count
        report["continuations"].extend(
            {
                "finding_id": finding_id,
                "iteration": index,
                "temporary_local_commit": True,
            }
            for index in range(1, continuation_count + 1)
        )
        current_finding += 1
        report["results"].append(record)
        if progress is not None:
            completed_phase = "uncertain" if record.get("deferred_uncertain") else "fixed"
            progress(
                {
                    "phase": completed_phase,
                    "current": current_finding,
                    "total": total_findings,
                    "finding_id": finding_id,
                    "commit": record.get("commit", ""),
                }
            )

    known_generation_findings = (
        (
            isinstance(open_findings, int)
            and not isinstance(open_findings, bool)
            and open_findings > 0
        )
        or reviewed_findings > 0
        or len(report["results"]) > generation_result_start
    )
    closure = _final_closure(
        root,
        env=env,
        state_root=state_root,
        push_mode=push_mode,
        branch=selected_branch,
        pushed=pushed,
        publish_clawpatch_state=publish_clawpatch_state,
        review_limit=max(mapped_features, 1),
        progress=progress,
        current=current_finding,
        total=total_findings,
        require_project_gates=require_project_gates,
        require_fresh_review=known_generation_findings,
        resolve_uncertain=not advance_uncertain,
    )
    report["results"].extend(closure.get("recovered_findings", []))
    report["continuations"].extend(closure.get("recovered_continuations", []))
    generation_head = _git_text(root, ["git", "rev-parse", "HEAD"])
    generation_tree = _git_text(root, ["git", "rev-parse", "HEAD^{tree}"])
    needs_fresh_review = bool(closure.get("needs_fresh_review", False))
    generation_record = {
        "generation": _fixed_point_generation,
        "head": generation_head,
        "source_tree": generation_tree,
        "mapped_features": mapped_features,
        "reviewed_features": review.get("review", {}).get("reviewed", 0),
        "review_findings": reviewed_findings,
        "completed_findings": len(report["results"]) - generation_result_start,
        "clean": not needs_fresh_review,
    }
    report["review_generations"].append(generation_record)
    if needs_fresh_review:
        if generation_tree in _fixed_point_seen_trees:
            raise SafetyError(
                "Fresh Clawpatch review did not converge: a non-clean generation repeated "
                f"source tree {generation_tree}. The supervisor stopped without starting another "
                "review generation."
            )
        if progress is not None:
            progress(
                {
                    "phase": "fixed-point-rescan",
                    "current": current_finding,
                    "total": total_findings,
                    "command": "start fresh ClawPatch map and complete review",
                    "attempt": _fixed_point_generation + 1,
                }
            )
        _prepare_fresh_release(
            root,
            env=env,
            progress=progress,
            state_root=state_root,
        )
        return _release_sweep_locked(
            root,
            apply=True,
            branch="current",
            push_mode=push_mode,
            publish_clawpatch_state=publish_clawpatch_state,
            trusted_host_codex_sandbox_bypass=trusted_host_codex_sandbox_bypass,
            fresh=False,
            child_timeout_seconds=child_timeout_seconds,
            progress=progress,
            integration_mode=integration_mode,
            child_env_overrides=child_env_overrides,
            advance_uncertain=advance_uncertain,
            _fixed_point_generation=_fixed_point_generation + 1,
            _fixed_point_seen_trees=(*_fixed_point_seen_trees, generation_tree),
            _prior_results=tuple(report["results"]),
            _prior_continuations=tuple(report["continuations"]),
            _prior_false_positives=tuple(report["false_positives"]),
            _prior_review_generations=tuple(report["review_generations"]),
            _already_pushed=bool(closure.get("pushed", pushed)),
        )
    final_head = _git_text(root, ["git", "rev-parse", "HEAD"])
    final_uncertain_count = _required_int(
        closure.get("uncertain_report", {"total": 0}), "total"
    )
    completion_status = "COMPLETE" if final_uncertain_count == 0 else "PROCESSED_WITH_UNCERTAIN"
    proof = {
        "status": completion_status,
        "completed_at": utc_now(),
        "repo": str(root),
        "branch": selected_branch,
        "git_head": final_head,
        "clawpatch_version": version,
        "open_findings": 0,
        "uncertain_findings": final_uncertain_count,
        "completed_findings": report["results"],
        "continuation_attempts": report["continuations"],
        "false_positives": report["false_positives"],
        "review_generations": report["review_generations"],
        "final_closure": closure,
    }
    proof_path = state_root / "clawpatch-release-proof.json"
    atomic_write_json(proof_path, proof)
    report.update(
        {
            "branch": selected_branch,
            "git_head": final_head,
            "finding_count": len(report["results"]),
            "false_positive_count": len(report["false_positives"]),
            "unresolved_count": final_uncertain_count,
            "open_findings": 0,
            "uncertain_findings": final_uncertain_count,
            "final_closure": closure,
            "proof_path": str(proof_path),
        }
    )
    return report


def format_release_sweep(report: dict[str, Any]) -> str:
    if not report.get("apply"):
        return (
            "CLAWPATCH RELEASE SWEEP PLAN\n"
            f"Repo: {report['repo']}\n"
            f"Clawpatch: {report['clawpatch_version']}\n"
            f"Lifecycle: {report['lifecycle']}\n"
            "No repository changes were made. Run again with --apply to execute.\n"
        )
    completion = "COMPLETE" if not report.get("uncertain_findings") else "PROCESSED WITH UNCERTAIN"
    return (
        f"CLAWPATCH RELEASE SWEEP: {completion}\n"
        f"Findings processed: {report.get('finding_count', 0)}\n"
        f"Open findings: {report.get('open_findings', 0)}\n"
        f"Uncertain findings retained: {report.get('uncertain_findings', 0)}\n"
        f"Final HEAD: {report.get('git_head', '')}\n"
        f"Proof: {report.get('proof_path', '')}\n"
    )
