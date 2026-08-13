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
import time
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
from .errors import GateFailure, RepositoryBusyError, RuntimeBudgetExceeded, SafetyError
from . import checkpoint as checkpoint_component
from . import git_ops as git_component
from . import proof as proof_component
from . import queue as queue_component
from . import validation as validation_component
from .git_ops import DirtySourcePolicy
from .proof import write_completion_proof
from .runner import CommandRunner
from .util import atomic_write_json, redact_argv, redact_text, utc_now


def _component_ops(*names: str) -> dict[str, Any]:
    """Bind facade operations at call time so adapter and test patches remain exact."""
    namespace = globals()
    return {name: namespace[name] for name in names}

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
    "unsupported or failed transitions stop with source changes intact; uncertain repairs remain in "
    "the same-finding loop and can never produce successful completion proof; a ClawPatch-owned false-positive "
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
        "CLAWPATCH_SUPERVISE_DEADLINE_MONOTONIC",
    }
)
_PYTHON_IMPORT_ENV_NAMES = frozenset({"PYTHONHOME", "PYTHONPATH"})
_PROCESS_CONTROL_ENV_NAMES = frozenset(
    {
        "BASH_ENV",
        "COMSPEC",
        "DOTNET_STARTUP_HOOKS",
        "ENV",
        "JDK_JAVA_OPTIONS",
        "JAVA_TOOL_OPTIONS",
        "KSHENV",
        "LIBPATH",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PATH",
        "PATHEXT",
        "PERL5LIB",
        "PERL5OPT",
        "PHPRC",
        "PHP_INI_SCAN_DIR",
        "RUBYLIB",
        "RUBYOPT",
        "SHELL",
        "SHLIB_PATH",
        "ZDOTDIR",
        "_JAVA_OPTIONS",
    }
)
_PROCESS_CONTROL_ENV_PREFIXES = ("DYLD_", "LD_")
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


def _clawpatch_control_env_overrides(
    *sources: dict[str, str],
) -> dict[str, str]:
    overrides = {name: value for source in sources for name, value in source.items()}
    for name, value in overrides.items():
        if not _ENV_NAME.fullmatch(name) or "\x00" in value:
            raise SafetyError(
                "The supervisor received an invalid validation-service environment value."
            )
        if name.upper() in _PYTHON_IMPORT_ENV_NAMES:
            raise SafetyError(
                f"Validation services cannot override Python import environment variable {name}."
            )
        normalized_name = name.upper()
        if normalized_name in _CLAWPATCH_POLICY_ENV_NAMES:
            raise SafetyError(
                f"Validation services cannot override policy-owned supervisor variable {name}."
            )
        if normalized_name in _PROCESS_CONTROL_ENV_NAMES or normalized_name.startswith(
            _PROCESS_CONTROL_ENV_PREFIXES
        ):
            raise SafetyError(
                f"Validation services cannot override process-control environment variable {name}."
            )
    return overrides


def _clawpatch_supervisor_path_override(source: dict[str, str] | None) -> str | None:
    if not source:
        return None
    if set(source) != {"PATH"}:
        raise SafetyError("The supervisor received an unexpected preflight environment override.")
    path = source["PATH"]
    if not path or "\x00" in path:
        raise SafetyError("The supervisor received an invalid preflight PATH override.")
    return path


def _clawpatch_validation_path_override(
    source: dict[str, str],
    *,
    temporary_root: Path,
    supervisor_path_override: str | None,
) -> str | None:
    virtual_env = source.get("VIRTUAL_ENV")
    if virtual_env is None:
        return supervisor_path_override
    if not virtual_env or "\x00" in virtual_env:
        raise SafetyError("The supervisor received an invalid validation Python environment.")
    try:
        trusted_root = temporary_root.resolve(strict=True)
        environment = Path(virtual_env)
        if not environment.is_absolute() or environment.is_symlink():
            raise ValueError
        resolved_environment = environment.resolve(strict=True)
        unresolved_executable_dir = resolved_environment / (
            "Scripts" if os.name == "nt" else "bin"
        )
        if unresolved_executable_dir.is_symlink():
            raise ValueError
        executable_dir = unresolved_executable_dir.resolve(strict=True)
        executable_dir.relative_to(trusted_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise SafetyError(
            "The validation Python environment is outside the supervisor-owned temporary root."
        ) from exc
    if executable_dir.parent != resolved_environment:
        raise SafetyError(
            "The validation Python environment is outside the supervisor-owned temporary root."
        )
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    if not executable_dir.is_dir() or not python.is_file():
        raise SafetyError("The supervisor received an incomplete validation Python environment.")
    trusted_path = supervisor_path_override
    if trusted_path is None:
        trusted_path = os.environ.get("PATH")
    return str(executable_dir) + (os.pathsep + trusted_path if trusted_path else "")


def _release_clawpatch_env(
    *,
    trusted_host_codex_sandbox_bypass: bool,
    allow_sandbox_bypass_fallback: bool = False,
    child_timeout_seconds: int = CLAWPATCH_CHILD_WATCHDOG_SECONDS,
    deadline_monotonic: float | None = None,
    child_env_overrides: dict[str, str] | None = None,
    supervisor_path_override: str | None = None,
) -> dict[str, str]:
    if child_timeout_seconds < 60:
        raise SafetyError("Clawpatch child timeout must be at least 60 seconds.")
    child_env = {
        name: value
        for name in _RELEASE_CHILD_INHERITED_ENV_NAMES
        if (value := os.environ.get(name)) is not None
    }
    overrides = _clawpatch_control_env_overrides(child_env_overrides or {})
    if supervisor_path_override is not None:
        if not supervisor_path_override or "\x00" in supervisor_path_override:
            raise SafetyError("The supervisor received an invalid preflight PATH override.")
        child_env["PATH"] = supervisor_path_override
    child_env["CLAWPATCH_CODEX_TIMEOUT_MS"] = str(child_timeout_seconds * 1_000)
    child_env["MANAGEROO_CLAWPATCH_CHILD_TIMEOUT_SECONDS"] = str(child_timeout_seconds)
    if deadline_monotonic is not None:
        if deadline_monotonic <= time.monotonic():
            raise RuntimeBudgetExceeded("The supervisor's total runtime budget is exhausted.")
        child_env["CLAWPATCH_SUPERVISE_DEADLINE_MONOTONIC"] = repr(deadline_monotonic)
    child_env.pop("MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK", None)
    child_env.pop("CLAWPATCH_CODEX_SANDBOX", None)
    if trusted_host_codex_sandbox_bypass:
        child_env["CLAWPATCH_CODEX_SANDBOX"] = "bypass"
    elif allow_sandbox_bypass_fallback:
        child_env["MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK"] = "1"
    for name, value in overrides.items():
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
    deadline_raw = env.get("CLAWPATCH_SUPERVISE_DEADLINE_MONOTONIC")
    if deadline_raw is None:
        return value
    try:
        remaining = int(float(deadline_raw) - time.monotonic())
    except ValueError as exc:
        raise SafetyError("The supervisor received an invalid total runtime deadline.") from exc
    if remaining < 60:
        raise RuntimeBudgetExceeded(
            "The supervisor's total runtime budget has less than 60 seconds remaining."
        )
    return min(value, remaining)


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int = 1800,
    env: dict[str, str] | None = None,
    kill_process_group: bool = True,
    timeout_start_barrier: Callable[[subprocess.Popen[str]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = _platform_command(
        argv,
        platform_name=os.name,
        path=env.get("PATH") if env is not None else None,
    )
    result = CommandRunner().run(
        command,
        cwd=cwd,
        timeout_seconds=timeout,
        env=env,
        kill_process_group=kill_process_group,
        errors="surrogateescape",
        timeout_start_barrier=timeout_start_barrier,
    )
    stderr = result.stderr
    if result.timed_out:
        stderr = stderr + ("\n" if stderr else "") + "TIMEOUT"
    return subprocess.CompletedProcess(command, result.exit_code, result.stdout, stderr)


def _platform_command(
    argv: list[str],
    *,
    platform_name: str,
    path: str | None = None,
) -> list[str]:
    command = list(argv)
    if platform_name == "nt" and command:
        executable = PureWindowsPath(command[0]).name.lower()
        if executable in {"clawpatch", "clawpatch.exe", "clawpatch.cmd", "clawpatch.bat"}:
            resolved = shutil.which(command[0], path=path) or shutil.which(
                "clawpatch", path=path
            )
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
        safe_argv = redact_argv(argv)
        safe_stdout = redact_text(result.stdout or "")
        safe_stderr = redact_text(result.stderr or "")
        raise SafetyError(
            f"command: {shlex.join(safe_argv)}\nexit code: {result.returncode}\n"
            f"failed requirement: command must exit 0\n"
            f"stdout:\n{safe_stdout[-4000:]}\nstderr:\n{safe_stderr[-4000:]}"
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
    return git_component._impl_git_root(
        _component_ops('Path', 'SafetyError', '_run'),
        repo,
    )


def _git_text(repo: Path, argv: list[str]) -> str:
    return git_component._impl_git_text(
        _component_ops('_must_run'),
        repo,
        argv,
    )


def _require_branch(repo: Path, expected: str, *, phase: str) -> None:
    return git_component._impl_require_branch(
        _component_ops('SafetyError', '_git_text'),
        repo,
        expected,
        phase=phase,
    )


def _clawpatch_state_fingerprint(root: Path) -> str:
    return git_component._impl_clawpatch_state_fingerprint(
        _component_ops('SafetyError', 'hashlib', 'os', 'stat'),
        root,
    )


def _hard_reset_preserving_clawpatch_state(repo: Path, target: str) -> None:
    return git_component._impl_hard_reset_preserving_clawpatch_state(
        _component_ops(
            'Path', 'SafetyError', '_clawpatch_state_fingerprint', '_must_run',
            'shutil', 'tempfile',
        ),
        repo,
        target,
    )


def _require_synchronized_remote_branch(
    repo: Path,
    branch: str,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    preserve_local_on_conflict: bool = False,
) -> str:
    return git_component._impl_require_synchronized_remote_branch(
        _component_ops(
            'RepositoryBusyError', 'SafetyError', '_git_text', '_hard_reset_preserving_clawpatch_state',
            '_must_run', '_require_branch', '_run', '_source_paths',
            'tempfile',
        ),
        repo,
        branch,
        progress=progress,
        preserve_local_on_conflict=preserve_local_on_conflict,
    )


def _status_entries(repo: Path) -> list[tuple[str, str]]:
    return git_component._impl_status_entries(
        _component_ops('SafetyError', '_must_run'),
        repo,
    )


def _status_paths(repo: Path) -> list[str]:
    return git_component._impl_status_paths(
        _component_ops('_status_entries'),
        repo,
    )


def _is_untracked_dependency_path(status: str, path: str) -> bool:
    return git_component._impl_is_untracked_dependency_path(
        _component_ops('PurePosixPath'),
        status,
        path,
    )


def _source_paths(repo: Path) -> list[str]:
    return git_component._impl_source_paths(
        _component_ops('_is_untracked_dependency_path', '_status_entries'),
        repo,
    )


def _normalized_stopped_owned_paths(
    repo: Path,
    checkpoint: dict[str, Any],
    recorded_paths: list[str],
) -> list[str]:
    return git_component._impl_normalized_stopped_owned_paths(
        _component_ops(
            '_is_untracked_dependency_path', '_source_paths', '_source_paths_fingerprint', '_status_entries',
        ),
        repo,
        checkpoint,
        recorded_paths,
    )


def _gitlink_paths(repo: Path) -> list[str]:
    return git_component._impl_gitlink_paths(
        _component_ops('SafetyError', '_must_run'),
        repo,
    )


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
    if first in {"python", "python.exe", "python3", "python3.exe"}:
        no_value_options = {
            "-b",
            "-bb",
            "-B",
            "-d",
            "-E",
            "-i",
            "-I",
            "-O",
            "-OO",
            "-P",
            "-q",
            "-s",
            "-S",
            "-u",
            "-v",
            "-x",
        }
        index = 1
        while index < len(argv):
            value = argv[index]
            if value == "-m":
                return index + 1 < len(argv) and argv[index + 1] in {
                    "clawpatch_supervise",
                    "clawpatch_supervise.clawpatch_external",
                    "manageroo.clawpatch_external",
                }
            if value == "-c":
                return False
            if value in {"-W", "-X", "--check-hash-based-pycs"}:
                index += 2
                continue
            if (value.startswith("-W") or value.startswith("-X")) and len(value) > 2:
                index += 1
                continue
            if value in no_value_options:
                index += 1
                continue
            if value == "--":
                index += 1
                break
            if value.startswith("-"):
                return False
            break
        script = argv[index] if index < len(argv) else ""
        return _command_name(script) in commands
    script = next((value for value in argv[1:4] if not value.startswith("-")), "")
    return _command_name(script) in commands


def _process_repository_root(cwd: Path) -> Path | None:
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=30)
    if result.returncode or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _clawpatch_process_repository_root(argv: list[str], cwd: Path) -> Path | None:
    supervisor_tokens = {
        "clawpatch-supervise",
        "clawpatch-supervise.exe",
        "clawpatch_supervise",
        "clawpatch_supervise.clawpatch_external",
        "manageroo.clawpatch_external",
    }
    is_supervisor = any(
        value in supervisor_tokens or _command_name(value) in supervisor_tokens
        for value in argv
    )
    declared_repo: str | None = None
    if is_supervisor:
        for index, value in enumerate(argv):
            if value == "--repo" and index + 1 < len(argv):
                declared_repo = argv[index + 1]
            elif value.startswith("--repo="):
                declared_repo = value.partition("=")[2]
    candidate = Path(declared_repo) if declared_repo is not None else cwd
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return _process_repository_root(candidate)


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
            if _clawpatch_process_repository_root(argv, cwd) == root:
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
        if cwd is not None and _clawpatch_process_repository_root(argv, cwd) == root:
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
        f"$_.ProcessId -ne $PID -and $_.ProcessId -ne {os.getpid()} -and $_.CommandLine"
        " }; "
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
        command = str(row.get("CommandLine") or "").strip()
        try:
            argv = [
                value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value
                for value in shlex.split(command, posix=False)
            ]
        except ValueError:
            argv = []
        if not _is_clawpatch_argv(argv):
            continue
        declared_roots = [
            next(value for value in match.groups() if value is not None)
            for match in re.finditer(
                r'(?i)(?:^|\s)--(?:repo|root)(?:=|\s+)(?:"([^"]+)"|(\S+))',
                command,
            )
        ]
        process_root = None
        if len(declared_roots) == 1 and PureWindowsPath(declared_roots[0]).is_absolute():
            process_root = _process_repository_root(Path(declared_roots[0]))
        if process_root is None:
            raise SafetyError(
                f"Could not establish repository ownership for Clawpatch process "
                f"{row.get('ProcessId')}."
            )
        if str(PureWindowsPath(process_root)).rstrip("\\/").casefold() != str(
            PureWindowsPath(root)
        ).rstrip("\\/").casefold():
            continue
        found.append(
            {
                "pid": row.get("ProcessId"),
                "cwd": str(process_root),
                "command": command,
            }
        )
    return found


def _require_no_process(repo: Path) -> None:
    active = _active_clawpatch_processes(repo)
    if active:
        raise RepositoryBusyError(
            f"A Clawpatch process is already active for this repository: {active}"
        )


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
                raise RepositoryBusyError(
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
    return validation_component._impl_clawpatch_doctor(
        _component_ops('CommandRunner', 'SafetyError', 'json'),
        repo,
        env=env,
    )


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
    return validation_component._impl_runtime_doctor(
        _component_ops(
            '_clawpatch_doctor', '_clawpatch_version', '_git_root', '_must_run',
            '_windows_codex_sandbox_path', 'os', 'sys',
        ),
        repo,
    )


def require_external_clawpatch_preflight(repo: Path) -> dict[str, str]:
    return validation_component._impl_require_external_clawpatch_preflight(
        _component_ops('_git_root', '_git_text', '_require_no_process', 'runtime_doctor'),
        repo,
    )


def _version_tuple(text: str) -> tuple[int, int, int]:
    return validation_component._impl_version_tuple(
        _component_ops('SafetyError', 're'),
        text,
    )


def _clawpatch_version(repo: Path) -> str:
    return validation_component._impl_clawpatch_version(
        _component_ops(
            'MINIMUM_CLAWPATCH_VERSION', 'SafetyError', '_must_run', '_version_tuple',
            'shutil',
        ),
        repo,
    )


def _run_clawpatch(
    repo: Path,
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    return validation_component._impl_run_clawpatch(
        _component_ops('_child_timeout_seconds', '_require_no_process', '_run'),
        repo,
        argv,
        env=env,
        timeout=timeout,
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
    return validation_component._impl_must_clawpatch(
        _component_ops(
            'SafetyError', '_ClawpatchCommandFailure', '_MissingFinding', '_child_timeout_seconds',
            '_clawpatch_command_phase', '_run_clawpatch', '_source_paths', 'classify_clawpatch_failure',
            'redact_text', 'shlex',
        ),
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


def _clawpatch_command_phase(argv: list[str]) -> str:
    return validation_component._impl_clawpatch_command_phase(
        _component_ops(),
        argv,
    )


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
    return validation_component._impl_json_clawpatch(
        _component_ops('_must_clawpatch', '_parse_json_output'),
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


def _next_finding(
    repo: Path,
    *,
    env: dict[str, str],
    status: str = "open",
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> tuple[str | None, dict[str, Any]]:
    return queue_component._impl_next_finding(
        _component_ops('SafetyError', '_FINDING_ID', '_json_clawpatch'),
        repo,
        env=env,
        status=status,
        progress=progress,
        current=current,
        total=total,
    )


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
    return queue_component._impl_show_finding(
        _component_ops('SafetyError', '_json_clawpatch'),
        repo,
        finding_id,
        env=env,
        required_status=required_status,
        progress=progress,
        current=current,
        total=total,
    )


def _finding_from_fix_argv(argv: list[str]) -> str:
    return queue_component._impl_finding_from_fix_argv(
        _component_ops('SafetyError', '_FINDING_ID'),
        argv,
    )


def _with_json(argv: list[str]) -> list[str]:
    return queue_component._impl_with_json(
        _component_ops(),
        argv,
    )


def _fix_command(
    repo: Path, argv: list[str], *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    return queue_component._impl_fix_command(
        _component_ops(
            'SafetyError', '_UnresolvedFinding', '_finding_from_fix_argv', '_parse_json_output',
            '_run_clawpatch', '_source_paths', '_with_json', 'classify_clawpatch_failure',
            'os', 'shlex',
        ),
        repo,
        argv,
        env=env,
    )


def _patch_attempt_from_show(
    show_payload: dict[str, Any], patch_attempt_id: str, finding_id: str
) -> dict[str, Any]:
    return queue_component._impl_patch_attempt_from_show(
        _component_ops('SafetyError'),
        show_payload,
        patch_attempt_id,
        finding_id,
    )


def _validate_attempt_paths(repo: Path, files: list[str]) -> None:
    return queue_component._impl_validate_attempt_paths(
        _component_ops('SafetyError', '_source_paths', '_validate_attempt_paths_syntax'),
        repo,
        files,
    )


def _run_project_gates(
    repo: Path,
    *,
    finding_id: str,
    required: bool = True,
) -> list[dict[str, Any]]:
    return validation_component._impl_run_project_gates(
        _component_ops(
            'CommandRunner', 'GateFailure', 'PROJECT_DIR', 'Path',
            'SafetyError', '_source_paths', 'shlex', 'tomllib',
        ),
        repo,
        finding_id=finding_id,
        required=required,
    )


def _source_state_fingerprint_for_paths(repo: Path, paths: list[str]) -> dict[str, Any]:
    return git_component._impl_source_state_fingerprint_for_paths(
        _component_ops(
            'SafetyError', '_git_text', '_gitlink_paths', '_must_run',
            '_source_state_fingerprint', '_untracked_path_fingerprint',
        ),
        repo,
        paths,
    )


def _source_state_fingerprint(repo: Path) -> dict[str, Any]:
    return git_component._impl_source_state_fingerprint(
        _component_ops('_source_paths', '_source_state_fingerprint_for_paths'),
        repo,
    )


def _untracked_path_fingerprint(repo: Path, path: str) -> str:
    return git_component._impl_untracked_path_fingerprint(
        _component_ops('_git_text', 'hashlib', 'os'),
        repo,
        path,
    )


def _owned_source_fingerprint(repo: Path, paths: list[str]) -> str:
    return git_component._impl_owned_source_fingerprint(
        _component_ops('_source_paths', '_source_paths_fingerprint'),
        repo,
        paths,
    )


def _source_paths_fingerprint(repo: Path, paths: list[str]) -> str:
    return git_component._impl_source_paths_fingerprint(
        _component_ops('_source_state_fingerprint_for_paths', 'hashlib', 'json'),
        repo,
        paths,
    )


def _legacy_owned_source_fingerprint(repo: Path, paths: list[str]) -> str:
    return git_component._impl_legacy_owned_source_fingerprint(
        _component_ops('_source_paths', '_source_state_fingerprint', 'hashlib', 'json'),
        repo,
        paths,
    )


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
    return validation_component._impl_revalidation_payload(
        _component_ops('SafetyError', '_json_clawpatch', '_source_paths', 'json', 'shlex'),
        repo,
        finding_id,
        env=env,
        progress=progress,
        phase=phase,
        current=current,
        total=total,
    )


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
    return validation_component._impl_revalidate(
        _component_ops(
            'SafetyError', '_ClawpatchCommandFailure', '_UnresolvedFinding', '_revalidation_payload',
            '_source_paths', '_source_state_fingerprint', 'classify_clawpatch_failure', 'json',
            'shlex',
        ),
        repo,
        finding_id,
        env=env,
        expected_paths=expected_paths,
        progress=progress,
        current=current,
        total=total,
    )


def _external_state_home() -> Path:
    return checkpoint_component._impl_external_state_home(
        _component_ops('Path', 'os'),
    )


def _legacy_external_state_homes() -> tuple[Path, ...]:
    return checkpoint_component._impl_legacy_external_state_homes(
        _component_ops('Path', '_external_state_home', 'os', 'sys'),
    )


def _repository_state_root(home: Path, repo: Path) -> Path:
    return checkpoint_component._impl_repository_state_root(
        _component_ops('hashlib', 'os'),
        home,
        repo,
    )


def _release_state_root(repo: Path, *, integration_mode: str) -> Path:
    return checkpoint_component._impl_release_state_root(
        _component_ops(
            'PROJECT_DIR', 'SafetyError', '_external_state_home', '_repository_state_root',
        ),
        repo,
        integration_mode=integration_mode,
    )


def external_state_root(repo: Path) -> Path:
    return checkpoint_component._impl_external_state_root(
        _component_ops('_release_state_root'),
        repo,
    )


def _release_progress_path(repo: Path, *, state_root: Path | None = None) -> Path:
    return checkpoint_component._impl_release_progress_path(
        _component_ops('PROJECT_DIR'),
        repo,
        state_root=state_root,
    )


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
    return checkpoint_component._impl_write_release_progress(
        _component_ops(
            'RELEASE_PROGRESS_VERSION', 'RepairAction', 'SafetyError', '_FINDING_ID',
            '_owned_source_fingerprint', '_release_progress_path', '_validate_attempt_paths_syntax', 'atomic_write_json',
            're', 'utc_now',
        ),
        repo,
        finding_id=finding_id,
        branch=branch,
        head_before=head_before,
        phase=phase,
        owned_paths=owned_paths,
        temporary_commit=temporary_commit,
        source_states=source_states,
        last_action=last_action,
        state_root=state_root,
    )


def _load_release_progress(
    repo: Path,
    *,
    state_root: Path | None = None,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_load_release_progress(
        _component_ops(
            'Path', 'RELEASE_PROGRESS_VERSION', 'RepairAction', 'SafetyError',
            '_FINDING_ID', '_release_progress_path', '_validate_attempt_paths_syntax', 'json',
            're',
        ),
        repo,
        state_root=state_root,
    )


def _migrate_legacy_external_progress(repo: Path, *, state_root: Path) -> None:
    return checkpoint_component._impl_migrate_legacy_external_progress(
        _component_ops(
            'PROJECT_DIR', 'RELEASE_PROGRESS_VERSION', 'SafetyError', '_legacy_external_state_homes',
            '_legacy_owned_source_fingerprint', '_load_release_progress', '_owned_source_fingerprint', '_release_progress_path',
            '_repository_state_root', 'atomic_write_json',
        ),
        repo,
        state_root=state_root,
    )


def _checkpoint_can_follow_supervisor_upgrade(
    repo: Path,
    progress: dict[str, Any],
) -> bool:
    return checkpoint_component._impl_checkpoint_can_follow_supervisor_upgrade(
        _component_ops('_SUPERVISOR_UPGRADE_PATHS', '_git_text', '_must_run', '_run', 'json'),
        repo,
        progress,
    )


def _checkpoint_completed_commit(
    repo: Path,
    progress: dict[str, Any],
) -> str:
    return checkpoint_component._impl_checkpoint_completed_commit(
        _component_ops('_git_text', '_must_run', '_run'),
        repo,
        progress,
    )


def _clean_descendant_retires_verified_checkpoint(
    repo: Path,
    progress: dict[str, Any],
) -> bool:
    return checkpoint_component._impl_clean_descendant_retires_verified_checkpoint(
        _component_ops(
            'SafetyError', '_git_text', '_run', '_source_paths',
            '_verify_iteration_commit', 'json',
        ),
        repo,
        progress,
    )


def _checkpoint_unapplied_attempt(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_checkpoint_unapplied_attempt(
        _component_ops('RepairAction', '_git_text', '_show_finding', '_source_paths'),
        repo,
        progress_record,
        env=env,
        progress=progress,
        inspected=inspected,
    )


def _recover_interrupted_source_clean_fix(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    state_root: Path,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_recover_interrupted_source_clean_fix(
        _component_ops(
            'RepairAction', '_git_text', '_run', '_source_paths',
            '_write_release_progress',
        ),
        repo,
        progress_record,
        state_root=state_root,
    )


def _attempt_base_preserves_owned_source(
    repo: Path,
    *,
    attempt_base: Any,
    current_head: str,
    owned_paths: list[str],
) -> bool:
    return checkpoint_component._impl_attempt_base_preserves_owned_source(
        _component_ops('_run', 're'),
        repo,
        attempt_base=attempt_base,
        current_head=current_head,
        owned_paths=owned_paths,
    )


def _checkpoint_same_finding_later_applied_attempt(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    inspected: dict[str, Any],
) -> dict[str, Any] | None:
    return checkpoint_component._impl_checkpoint_same_finding_later_applied_attempt(
        _component_ops(
            'SafetyError', '_attempt_base_preserves_owned_source', '_git_text', '_parse_checkpoint_time',
            '_run', '_source_paths', '_validate_attempt_paths', '_validate_attempt_paths_syntax',
            '_verify_iteration_commit', 'datetime', 'timezone',
        ),
        repo,
        progress_record,
        inspected=inspected,
    )


def _checkpoint_cross_finding_applied_attempt(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str] | None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_checkpoint_cross_finding_applied_attempt(
        _component_ops(
            'SafetyError', '_FINDING_ID', '_attempt_base_preserves_owned_source', '_git_text',
            '_parse_checkpoint_time', '_show_finding', '_source_paths', '_validate_attempt_paths',
            '_validate_attempt_paths_syntax', 'datetime', 'json', 'timezone',
        ),
        repo,
        progress_record,
        env=env,
        progress=progress,
    )


def _checkpoint_later_applied_attempt(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    inspected: dict[str, Any],
    env: dict[str, str] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_checkpoint_later_applied_attempt(
        _component_ops(
            '_checkpoint_cross_finding_applied_attempt', '_checkpoint_same_finding_later_applied_attempt',
        ),
        repo,
        progress_record,
        inspected=inspected,
        env=env,
        progress=progress,
    )


def _checkpoint_fixed_without_source(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_checkpoint_fixed_without_source(
        _component_ops('SafetyError', '_git_text', '_show_finding', '_source_paths'),
        repo,
        progress_record,
        env=env,
        progress=progress,
        inspected=inspected,
    )


def _checkpoint_false_positive_without_source(
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_checkpoint_false_positive_without_source(
        _component_ops(
            'RepairAction', 'SafetyError', '_git_text', '_show_finding',
            '_source_paths', '_verify_iteration_commit',
        ),
        repo,
        progress_record,
        env=env,
        progress=progress,
        inspected=inspected,
    )


def _clear_release_progress(repo: Path, *, state_root: Path | None = None) -> None:
    return checkpoint_component._impl_clear_release_progress(
        _component_ops('_release_progress_path'),
        repo,
        state_root=state_root,
    )


def recover_external_interrupted_state(
    repo: Path,
    *,
    reason: str,
    adopt_dirty: bool = False,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_recover_external_interrupted_state(
        _component_ops(
            'DirtySourcePolicy', 'SafetyError', '_clear_release_progress', '_commit_preexisting_source_baseline',
            '_git_text', '_load_release_progress', '_release_progress_path', '_source_paths',
            '_source_paths_fingerprint', 'atomic_write_json', 'external_state_root', 'hashlib',
            'json', 'utc_now',
        ),
        repo,
        reason=reason,
        adopt_dirty=adopt_dirty,
    )


def _parse_checkpoint_time(value: Any) -> datetime | None:
    return checkpoint_component._impl_parse_checkpoint_time(
        _component_ops('datetime', 'timezone'),
        value,
    )


def _empty_clawpatch_history(repo: Path) -> bool:
    return checkpoint_component._impl_empty_clawpatch_history(
        _component_ops(),
        repo,
    )


def _rebuilt_generation_owns_checkpoint_source(
    repo: Path,
    progress_record: dict[str, Any],
) -> bool:
    return checkpoint_component._impl_rebuilt_generation_owns_checkpoint_source(
        _component_ops(
            '_empty_clawpatch_history', '_git_text', '_owned_source_fingerprint', '_parse_checkpoint_time',
            '_source_paths', 'datetime', 'json', 'timezone',
        ),
        repo,
        progress_record,
    )


def _rebuilt_generation_supersedes_empty_checkpoint(
    repo: Path,
    progress_record: dict[str, Any],
) -> bool:
    return checkpoint_component._impl_rebuilt_generation_supersedes_empty_checkpoint(
        _component_ops(
            '_git_text', '_parse_checkpoint_time', '_run', '_source_paths',
            'json', 're',
        ),
        repo,
        progress_record,
    )


def _committed_clawpatch_config(repo: Path) -> str | None:
    return git_component._impl_committed_clawpatch_config(
        _component_ops('_run'),
        repo,
    )


def _exclude_gitlinks_from_clawpatch_config(repo: Path) -> list[str]:
    return checkpoint_component._impl_exclude_gitlinks_from_clawpatch_config(
        _component_ops('SafetyError', '_gitlink_paths', 'atomic_write_json', 'json'),
        repo,
    )


def _fresh_checkpoint_owned_paths(
    repo: Path,
    source_changes: list[str],
    *,
    state_root: Path | None = None,
) -> list[str]:
    return checkpoint_component._impl_fresh_checkpoint_owned_paths(
        _component_ops(
            'SafetyError', '_checkpoint_can_follow_supervisor_upgrade', '_checkpoint_proves_exact_source', '_git_text',
            '_load_release_progress', '_validate_attempt_paths_syntax',
        ),
        repo,
        source_changes,
        state_root=state_root,
    )


def _checkpoint_proves_exact_source(
    repo: Path,
    checkpoint: dict[str, Any],
    paths: list[str],
) -> bool:
    return checkpoint_component._impl_checkpoint_proves_exact_source(
        _component_ops(
            '_owned_source_fingerprint', '_source_paths', '_temporary_commit_matches_owned_source',
        ),
        repo,
        checkpoint,
        paths,
    )


def _commit_ambiguous_checkpoint_source_baseline(
    repo: Path,
    checkpoint: dict[str, Any],
    paths: list[str],
    *,
    state_root: Path,
) -> dict[str, Any] | None:
    return checkpoint_component._impl_commit_ambiguous_checkpoint_source_baseline(
        _component_ops(
            'Path', '_clear_release_progress', '_commit_preexisting_source_baseline', '_source_paths',
            'atomic_write_json', 'json',
        ),
        repo,
        checkpoint,
        paths,
        state_root=state_root,
    )


def _commit_preexisting_source_baseline(
    repo: Path,
    paths: list[str],
    *,
    state_root: Path,
) -> dict[str, Any]:
    return checkpoint_component._impl_commit_preexisting_source_baseline(
        _component_ops(
            'Path', 'SafetyError', '_git_text', '_must_run',
            '_paths_between', '_run', '_source_paths', '_source_paths_fingerprint',
            '_validate_attempt_paths_syntax', 'atomic_write_json', 'current_temporary_root', 'os',
            'tempfile', 'utc_now',
        ),
        repo,
        paths,
        state_root=state_root,
    )


def _current_input_baseline_commit(repo: Path) -> str:
    return git_component._impl_current_input_baseline_commit(
        _component_ops('_git_text', '_run'),
        repo,
    )


def _temporary_commit_matches_owned_source(
    repo: Path,
    *,
    original_head: str,
    temporary_commit: str,
    paths: list[str],
) -> bool:
    return git_component._impl_temporary_commit_matches_owned_source(
        _component_ops(
            'Path', '_git_text', '_must_run', 'current_temporary_root',
            'os', 'tempfile',
        ),
        repo,
        original_head=original_head,
        temporary_commit=temporary_commit,
        paths=paths,
    )


def _recover_checkpoint_temporary_commit(
    repo: Path,
    *,
    state_root: Path | None = None,
) -> None:
    return checkpoint_component._impl_recover_checkpoint_temporary_commit(
        _component_ops(
            'SafetyError', '_git_text', '_load_release_progress', '_must_run',
            '_run', '_source_paths', '_verify_iteration_commit',
        ),
        repo,
        state_root=state_root,
    )


def _validate_attempt_paths_syntax(paths: list[str]) -> None:
    return validation_component._impl_validate_attempt_paths_syntax(
        _component_ops('PurePosixPath', 'PureWindowsPath', 'SafetyError'),
        paths,
    )


def _discard_checkpoint_owned_source(repo: Path, paths: list[str]) -> None:
    return checkpoint_component._impl_discard_checkpoint_owned_source(
        _component_ops('SafetyError', '_must_run', '_source_paths'),
        repo,
        paths,
    )


def _prepare_fresh_release(
    repo: Path,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    state_root: Path | None = None,
) -> None:
    return proof_component._impl_prepare_fresh_release(
        _component_ops(
            'PROJECT_DIR', 'Path', 'SafetyError', '_clear_release_progress',
            '_committed_clawpatch_config', '_exclude_gitlinks_from_clawpatch_config', '_git_text', '_json_clawpatch',
            '_recover_checkpoint_temporary_commit', '_require_no_process', '_source_paths', 'shutil',
        ),
        repo,
        env=env,
        progress=progress,
        state_root=state_root,
    )


def _commit_attempt(
    repo: Path,
    finding_id: str,
    files: list[str],
    *,
    branch: str,
    outcome: str = "fixed",
) -> str:
    return git_component._impl_commit_attempt(
        _component_ops(
            'SafetyError', '_commit_without_local_hooks', '_git_text', '_must_run',
            '_require_branch', '_source_paths', '_validate_attempt_paths',
        ),
        repo,
        finding_id,
        files,
        branch=branch,
        outcome=outcome,
    )


def _commit_without_local_hooks(repo: Path, *args: str) -> None:
    return git_component._impl_commit_without_local_hooks(
        _component_ops('_must_run', 'current_temporary_root', 'tempfile'),
        repo,
        *args,
    )


def _paths_between(repo: Path, start: str, end: str = "HEAD") -> list[str]:
    return git_component._impl_paths_between(
        _component_ops('_must_run', '_validate_attempt_paths_syntax'),
        repo,
        start,
        end,
    )


def _verify_iteration_commit(
    repo: Path,
    *,
    finding_id: str,
    original_head: str,
    temporary_commit: str,
    require_current: bool = True,
) -> list[str]:
    return git_component._impl_verify_iteration_commit(
        _component_ops('SafetyError', '_git_text', '_paths_between'),
        repo,
        finding_id=finding_id,
        original_head=original_head,
        temporary_commit=temporary_commit,
        require_current=require_current,
    )


def _stage_current_source(repo: Path) -> tuple[list[str], str]:
    return git_component._impl_stage_current_source(
        _component_ops(
            'SafetyError', '_git_text', '_must_run', '_source_paths',
            '_validate_attempt_paths_syntax',
        ),
        repo,
    )


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
    return proof_component._impl_save_partial_iteration(
        _component_ops(
            'RepairAction', 'SafetyError', '_UnresolvedFinding', '_commit_without_local_hooks',
            '_git_text', '_require_branch', '_source_paths', '_stage_current_source',
            '_verify_iteration_commit', '_write_release_progress',
        ),
        repo,
        finding_id=finding_id,
        branch=branch,
        original_head=original_head,
        temporary_commit=temporary_commit,
        seen_states=seen_states,
        state_root=state_root,
    )


def _finalize_finding_commit(
    repo: Path,
    *,
    finding_id: str,
    branch: str,
    original_head: str,
    temporary_commit: str,
    seen_states: set[str],
) -> str:
    return git_component._impl_finalize_finding_commit(
        _component_ops(
            'SafetyError', '_UnresolvedFinding', '_commit_attempt', '_commit_without_local_hooks',
            '_git_text', '_paths_between', '_require_branch', '_source_paths',
            '_stage_current_source', '_verify_iteration_commit',
        ),
        repo,
        finding_id=finding_id,
        branch=branch,
        original_head=original_head,
        temporary_commit=temporary_commit,
        seen_states=seen_states,
    )


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
    return proof_component._impl_stop_finding_iteration(
        _component_ops(
            'SafetyError', '_git_text', '_must_run', '_source_paths',
            '_validate_attempt_paths_syntax', '_verify_iteration_commit', '_write_release_progress',
        ),
        repo,
        finding_id=finding_id,
        branch=branch,
        original_head=original_head,
        temporary_commit=temporary_commit,
        seen_states=seen_states,
        state_root=state_root,
        repair_action=repair_action,
    )


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
    return proof_component._impl_complete_fixed_finding(
        _component_ops(
            'SafetyError', '_clear_release_progress', '_finalize_finding_commit', '_git_text',
            '_paths_between', '_push_and_verify', '_source_paths', '_stop_finding_iteration',
            '_write_release_progress',
        ),
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
    return queue_component._impl_process_finding_until_fixed(
        _component_ops(
            'CLAWPATCH_ZERO_SOURCE_RETRY_LIMIT', 'ClawpatchFailureKind', 'RepairAction', 'SafetyError',
            '_UnresolvedFinding', '_clear_release_progress', '_complete_fixed_finding', '_discard_checkpoint_owned_source',
            '_execute_fix', '_git_text', '_paths_between', '_revalidate',
            '_run_project_gates', '_save_partial_iteration', '_source_paths', '_stop_finding_iteration',
            '_verify_iteration_commit', '_write_release_progress', 'decide_repair_transition', 'failure_from_legacy_outcome',
        ),
        repo,
        finding_id,
        inspected=inspected,
        env=env,
        push_mode=push_mode,
        branch=branch,
        pushed=pushed,
        state_root=state_root,
        progress=progress,
        current=current,
        total=total,
        require_project_gates=require_project_gates,
        resume_original_head=resume_original_head,
        resume_temporary_commit=resume_temporary_commit,
        resume_seen_states=resume_seen_states,
        resume_attempt=resume_attempt,
        resume_continuations=resume_continuations,
        advance_uncertain=advance_uncertain,
    )


def _push_and_verify(repo: Path, branch: str, *, first: bool) -> None:
    return git_component._impl_push_and_verify(
        _component_ops('SafetyError', '_git_text', '_must_run', '_require_branch'),
        repo,
        branch,
        first=first,
    )


def _publish_final_state(repo: Path, *, branch: str) -> str:
    return git_component._impl_publish_final_state(
        _component_ops(
            'SafetyError', '_git_text', '_must_run', '_require_branch',
            '_status_paths',
        ),
        repo,
        branch=branch,
    )


def _restore_committed_clawpatch_state(repo: Path) -> None:
    return proof_component._impl_restore_committed_clawpatch_state(
        _component_ops(
            'SafetyError', '_must_run', '_source_state_fingerprint', '_status_paths',
            'shutil',
        ),
        repo,
    )


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
    return queue_component._impl_execute_fix(
        _component_ops(
            'SafetyError', '_UnresolvedFinding', '_commit_attempt', '_fix_command',
            '_git_text', '_patch_attempt_from_show', '_push_and_verify', '_require_no_process',
            '_revalidate', '_run_project_gates', '_show_finding', '_source_paths',
            '_validate_attempt_paths',
        ),
        repo,
        finding_id,
        inspected=inspected,
        env=env,
        push_mode=push_mode,
        branch=branch,
        pushed=pushed,
        progress=progress,
        current=current,
        total=total,
        require_project_gates=require_project_gates,
        finalize=finalize,
    )


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
    return queue_component._impl_resume_stopped_attempt(
        _component_ops(
            'GateFailure', 'RepairAction', 'SafetyError', '_UnresolvedFinding',
            '_attempt_base_preserves_owned_source', '_commit_attempt', '_git_text', '_normalized_stopped_owned_paths',
            '_owned_source_fingerprint', '_push_and_verify', '_revalidate', '_run_project_gates',
            '_show_finding', '_source_paths', '_validate_attempt_paths', '_validate_attempt_paths_syntax',
            '_verify_iteration_commit', 're',
        ),
        repo,
        checkpoint,
        env=env,
        push_mode=push_mode,
        branch=branch,
        pushed=pushed,
        progress=progress,
        require_project_gates=require_project_gates,
        advance_uncertain=advance_uncertain,
    )


def _required_int(payload: dict[str, Any], field: str) -> int:
    return queue_component._impl_required_int(
        _component_ops('SafetyError'),
        payload,
        field,
    )


def _map_repository(
    repo: Path,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return validation_component._impl_map_repository(
        _component_ops('_json_clawpatch', '_required_int'),
        repo,
        env=env,
        progress=progress,
    )


def _review_probe(
    repo: Path,
    *,
    env: dict[str, str],
    review_limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> dict[str, Any]:
    return queue_component._impl_review_probe(
        _component_ops('SafetyError', '_json_clawpatch', '_required_int'),
        repo,
        env=env,
        review_limit=review_limit,
        progress=progress,
        current=current,
        total=total,
    )


def _review_completion(
    repo: Path,
    *,
    env: dict[str, str],
    review_limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return queue_component._impl_review_completion(
        _component_ops('SafetyError', '_required_int', '_review_probe'),
        repo,
        env=env,
        review_limit=review_limit,
        progress=progress,
    )


def _review_all_features(
    repo: Path,
    *,
    env: dict[str, str],
    mapped_features: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    return queue_component._impl_review_all_features(
        _component_ops('SafetyError', '_json_clawpatch', '_required_int', '_review_probe'),
        repo,
        env=env,
        mapped_features=mapped_features,
        progress=progress,
    )


def _resolve_uncertain_findings(
    repo: Path,
    *,
    env: dict[str, str],
    uncertain_total: int,
    require_project_gates: bool,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current_offset: int = 0,
    finding_ids: list[str] | None = None,
    retain_uncertain: bool = False,
) -> tuple[list[dict[str, Any]], list[str]]:
    return queue_component._impl_resolve_uncertain_findings(
        _component_ops(
            'SafetyError', '_FINDING_ID', '_next_finding', '_revalidate',
            '_run_project_gates', '_show_finding', '_source_paths',
        ),
        repo,
        env=env,
        uncertain_total=uncertain_total,
        require_project_gates=require_project_gates,
        progress=progress,
        current_offset=current_offset,
        finding_ids=finding_ids,
        retain_uncertain=retain_uncertain,
    )


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
    refresh_retained_uncertain: bool = False,
) -> dict[str, Any]:
    return proof_component._impl_final_closure(
        _component_ops(
            'SafetyError', '_json_clawpatch', '_next_finding', '_process_finding_until_fixed',
            '_publish_final_state', '_push_and_verify', '_require_no_process', '_required_int',
            '_resolve_uncertain_findings', '_review_completion', '_run_project_gates', '_show_finding',
            '_source_paths', '_status_paths',
        ),
        repo,
        env=env,
        state_root=state_root,
        push_mode=push_mode,
        branch=branch,
        pushed=pushed,
        publish_clawpatch_state=publish_clawpatch_state,
        review_limit=review_limit,
        progress=progress,
        current=current,
        total=total,
        require_project_gates=require_project_gates,
        require_fresh_review=require_fresh_review,
        resolve_uncertain=resolve_uncertain,
        refresh_retained_uncertain=refresh_retained_uncertain,
    )


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
    supervisor_path_override: str | None = None,
    advance_uncertain: bool = False,
    wait_on_preserved_source: bool = False,
    adopt_dirty: bool = False,
    deadline_monotonic: float | None = None,
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
            supervisor_path_override=supervisor_path_override,
            advance_uncertain=advance_uncertain,
            wait_on_preserved_source=wait_on_preserved_source,
            adopt_dirty=adopt_dirty,
            deadline_monotonic=deadline_monotonic,
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
            supervisor_path_override=supervisor_path_override,
            advance_uncertain=advance_uncertain,
            wait_on_preserved_source=wait_on_preserved_source,
            adopt_dirty=adopt_dirty,
            deadline_monotonic=deadline_monotonic,
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
    supervisor_path_override: str | None = None,
    advance_uncertain: bool = False,
    wait_on_preserved_source: bool = False,
    adopt_dirty: bool = False,
    deadline_monotonic: float | None = None,
    _fixed_point_generation: int = 1,
    _fixed_point_seen_trees: tuple[str, ...] = (),
    _prior_results: tuple[dict[str, Any], ...] = (),
    _prior_continuations: tuple[dict[str, Any], ...] = (),
    _prior_false_positives: tuple[dict[str, Any], ...] = (),
    _prior_review_generations: tuple[dict[str, Any], ...] = (),
    _already_pushed: bool = False,
    _preexisting_baseline_commit: str = "",
) -> dict[str, Any]:
    return queue_component._impl_release_sweep_locked(
        _component_ops(
            'ClawpatchStop', 'DirtySourcePolicy', 'LIFECYCLE', 'RepairAction',
            'RepositoryBusyError', 'SafetyError', '_MissingFinding', '_UnresolvedFinding',
            '_checkpoint_can_follow_supervisor_upgrade', '_checkpoint_completed_commit', '_checkpoint_cross_finding_applied_attempt', '_checkpoint_false_positive_without_source',
            '_checkpoint_fixed_without_source', '_checkpoint_later_applied_attempt', '_checkpoint_proves_exact_source', '_checkpoint_same_finding_later_applied_attempt',
            '_checkpoint_unapplied_attempt', '_clawpatch_version', '_clean_descendant_retires_verified_checkpoint', '_clear_release_progress',
            '_commit_ambiguous_checkpoint_source_baseline', '_commit_preexisting_source_baseline', '_current_input_baseline_commit', '_discard_checkpoint_owned_source',
            '_final_closure', '_git_root', '_git_text', '_json_clawpatch',
            '_load_release_progress', '_map_repository', '_migrate_legacy_external_progress', '_must_run',
            '_next_finding', '_prepare_fresh_release', '_process_finding_until_fixed', '_push_and_verify',
            '_rebuilt_generation_owns_checkpoint_source', '_rebuilt_generation_supersedes_empty_checkpoint', '_recover_interrupted_source_clean_fix', '_release_clawpatch_env',
            '_release_state_root', '_release_sweep_locked', '_require_no_process', '_require_synchronized_remote_branch',
            '_required_int', '_resume_stopped_attempt', '_review_all_features', '_run_project_gates',
            '_save_partial_iteration', '_show_finding', '_source_paths', '_stop_finding_iteration',
            '_write_release_progress', 'datetime', 'timezone', 'write_completion_proof',
        ),
        repo,
        apply=apply,
        branch=branch,
        push_mode=push_mode,
        publish_clawpatch_state=publish_clawpatch_state,
        trusted_host_codex_sandbox_bypass=trusted_host_codex_sandbox_bypass,
        fresh=fresh,
        child_timeout_seconds=child_timeout_seconds,
        progress=progress,
        integration_mode=integration_mode,
        child_env_overrides=child_env_overrides,
        supervisor_path_override=supervisor_path_override,
        advance_uncertain=advance_uncertain,
        wait_on_preserved_source=wait_on_preserved_source,
        adopt_dirty=adopt_dirty,
        deadline_monotonic=deadline_monotonic,
        _fixed_point_generation=_fixed_point_generation,
        _fixed_point_seen_trees=_fixed_point_seen_trees,
        _prior_results=_prior_results,
        _prior_continuations=_prior_continuations,
        _prior_false_positives=_prior_false_positives,
        _prior_review_generations=_prior_review_generations,
        _already_pushed=_already_pushed,
        _preexisting_baseline_commit=_preexisting_baseline_commit,
    )


def format_release_sweep(report: dict[str, Any]) -> str:
    if not report.get("apply"):
        return (
            "CLAWPATCH RELEASE SWEEP PLAN\n"
            f"Repo: {report['repo']}\n"
            f"Clawpatch: {report['clawpatch_version']}\n"
            f"Lifecycle: {report['lifecycle']}\n"
            "No repository changes were made. Run again with --apply to execute.\n"
        )
    queue_result = queue_component.QueueResult.from_report(report)
    completion = "COMPLETE" if report.get("ok") and queue_result.complete else "UNFINISHED"
    return (
        f"CLAWPATCH RELEASE SWEEP: {completion}\n"
        f"Findings processed: {report.get('finding_count', 0)}\n"
        f"Open findings: {report.get('open_findings', 0)}\n"
        f"Uncertain findings retained: {report.get('uncertain_findings', 0)}\n"
        f"Final HEAD: {report.get('git_head', '')}\n"
        f"Proof: {report.get('proof_path', '')}\n"
    )
