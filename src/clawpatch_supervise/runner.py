from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .errors import SafetyError
from .util import atomic_write_json, ensure_within, redact_argv, redact_text, utc_now


_CMD_ARGUMENT_METACHARACTERS = frozenset('&|<>()^%!"\r\n')


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    cwd: str
    started_at: str
    finished_at: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def to_dict(self) -> dict:
        return asdict(self)


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _prefer_timeout_text(current: str, candidate: str | bytes | None) -> str:
    value = _timeout_text(candidate)
    return value if len(value) >= len(current) else current


def _close_process_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdout, process.stderr, process.stdin):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    stdout: str = "",
    stderr: str = "",
) -> tuple[str, str]:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        cleanup_stdout, cleanup_stderr = process.communicate(timeout=5)
        stdout = _prefer_timeout_text(stdout, cleanup_stdout)
        stderr = _prefer_timeout_text(stderr, cleanup_stderr)
    except subprocess.TimeoutExpired as cleanup_timeout:
        stdout = _prefer_timeout_text(stdout, cleanup_timeout.stdout)
        stderr = _prefer_timeout_text(stderr, cleanup_timeout.stderr)
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        _close_process_pipes(process)
    return stdout, stderr


def _platform_argv(argv: Sequence[str], env: Mapping[str, str]) -> list[str] | str:
    resolved = list(argv)
    if os.name != "nt":
        return resolved
    program = resolved[0]
    discovered = (
        program
        if os.path.isabs(program) or "/" in program or "\\" in program
        else shutil.which(program, path=env.get("PATH"))
    )
    if discovered:
        resolved[0] = discovered
    if resolved[0].casefold().endswith((".cmd", ".bat")):
        if any(_CMD_ARGUMENT_METACHARACTERS.intersection(argument) for argument in resolved[1:]):
            raise SafetyError(
                "Windows batch command arguments cannot contain cmd.exe metacharacters."
            )
        command_line = subprocess.list2cmdline(resolved)
        launcher = subprocess.list2cmdline(
            [env.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c"]
        )
        return f'{launcher} "{command_line}"'
    return resolved


def _command_log_path(log_root: Path, log_name: str) -> Path:
    if (
        not isinstance(log_name, str)
        or not log_name
        or log_name in {".", ".."}
        or "/" in log_name
        or "\\" in log_name
        or "\x00" in log_name
    ):
        raise SafetyError("Command log names must be single filename components.")
    return ensure_within(log_root, log_root / f"{log_name}.json")


class CommandRunner:
    """Executes argv directly without a command shell."""

    def __init__(self, log_root: Path | None = None):
        self.log_root = log_root
        if log_root:
            log_root.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int = 1800,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        log_name: str | None = None,
        check: bool = False,
        kill_process_group: bool = True,
    ) -> CommandResult:
        """Run a command in an isolated process group by default.

        Set ``kill_process_group=False`` only for commands known not to spawn descendants.
        """
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise SafetyError("Commands must be non-empty argv arrays.")
        log_path = None
        if log_name is not None:
            if self.log_root is not None:
                log_path = _command_log_path(self.log_root, log_name)
            elif not isinstance(log_name, str) or not log_name:
                raise SafetyError("Command log names must be single filename components.")
        safe_argv = redact_argv(argv)
        started_at = utc_now()
        process_env = (
            os.environ.copy()
            if env is None
            else {str(k): str(v) for k, v in env.items()}
        )
        launch_argv = _platform_argv(argv, process_env)
        try:
            if kill_process_group:
                completed, timed_out = self._run_process_group(
                    launch_argv,
                    cwd=cwd,
                    env=process_env,
                    input_text=input_text,
                    timeout_seconds=timeout_seconds,
                )
            else:
                completed = subprocess.run(
                    launch_argv,
                    cwd=str(cwd),
                    env=process_env,
                    input=input_text,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=timeout_seconds,
                    shell=False,
                    check=False,
                )
                timed_out = False
            result = CommandResult(
                argv=safe_argv,
                cwd=str(cwd),
                started_at=started_at,
                finished_at=utc_now(),
                exit_code=124 if timed_out else completed.returncode,
                stdout=redact_text(completed.stdout),
                stderr=redact_text(completed.stderr),
                timed_out=timed_out,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(
                argv=safe_argv,
                cwd=str(cwd),
                started_at=started_at,
                finished_at=utc_now(),
                exit_code=124,
                stdout=redact_text(_timeout_text(exc.stdout)),
                stderr=redact_text(_timeout_text(exc.stderr)),
                timed_out=True,
            )
        except OSError as exc:
            result = CommandResult(
                argv=safe_argv,
                cwd=str(cwd),
                started_at=started_at,
                finished_at=utc_now(),
                exit_code=127,
                stdout="",
                stderr=redact_text(f"Could not launch command: {exc}"),
                timed_out=False,
            )
        if log_path is not None:
            atomic_write_json(log_path, result.to_dict())
        if check and not result.passed:
            raise subprocess.CalledProcessError(
                result.exit_code, result.argv, result.stdout, result.stderr
            )
        return result

    @staticmethod
    def _run_process_group(
        argv: list[str] | str,
        *,
        cwd: Path,
        env: Mapping[str, str],
        input_text: str | None,
        timeout_seconds: int,
    ) -> tuple[subprocess.CompletedProcess[str], bool]:
        kwargs: dict = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.PIPE if input_text is not None else None,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(argv, **kwargs)
        try:
            stdout, stderr = process.communicate(input=input_text, timeout=timeout_seconds)
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr), False
        except subprocess.TimeoutExpired as initial_timeout:
            stdout = _timeout_text(initial_timeout.stdout)
            stderr = _timeout_text(initial_timeout.stderr)
            stdout, stderr = _terminate_process_group(
                process,
                stdout=stdout,
                stderr=stderr,
            )
            return subprocess.CompletedProcess(argv, 124, stdout, stderr), True
        except BaseException:
            _terminate_process_group(process)
            raise
