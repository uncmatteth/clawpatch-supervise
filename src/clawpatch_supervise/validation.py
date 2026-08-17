from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import SafetyError


@dataclass(frozen=True)
class CompletionValidation:
    status: str
    open_findings: int
    uncertain_findings: int

    @classmethod
    def require_complete(
        cls, *, open_findings: int, uncertain_findings: int, allow_uncertain: bool = False
    ) -> CompletionValidation:
        if open_findings or (uncertain_findings and not allow_uncertain):
            raise SafetyError(
                "ClawPatch supervision is incomplete: "
                f"open={open_findings} uncertain={uncertain_findings}."
            )
        return cls(
            status="COMPLETE",
            open_findings=0,
            uncertain_findings=uncertain_findings,
        )


# Release-engine component implementations. The compatibility facade remains in clawpatch_release.

def _impl_clawpatch_doctor(
    ops: Any,
    repo: Path, *, env: dict[str, str] | None = None,
) -> dict[str, Any]:
    CommandRunner = ops['CommandRunner']
    SafetyError = ops['SafetyError']
    json = ops['json']
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


def _impl_runtime_doctor(
    ops: Any,
    repo: Path,
) -> tuple[dict[str, Any], dict[str, str]]:
    _clawpatch_doctor = ops['_clawpatch_doctor']
    _clawpatch_version = ops['_clawpatch_version']
    _git_root = ops['_git_root']
    _must_run = ops['_must_run']
    _windows_codex_sandbox_path = ops['_windows_codex_sandbox_path']
    os = ops['os']
    sys = ops['sys']
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


def _impl_require_external_clawpatch_preflight(
    ops: Any,
    repo: Path,
) -> dict[str, str]:
    _git_root = ops['_git_root']
    _git_text = ops['_git_text']
    _require_no_process = ops['_require_no_process']
    runtime_doctor = ops['runtime_doctor']
    """Prove tool, provider, Git, and process readiness before external service setup."""
    root = _git_root(repo)
    _report, env_overrides = runtime_doctor(root)
    _git_text(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    _git_text(root, ["git", "rev-parse", "HEAD"])
    _require_no_process(root)
    return env_overrides


def _impl_clawpatch_version(
    ops: Any,
    repo: Path,
) -> str:
    MINIMUM_CLAWPATCH_VERSION = ops['MINIMUM_CLAWPATCH_VERSION']
    SafetyError = ops['SafetyError']
    _must_run = ops['_must_run']
    _version_tuple = ops['_version_tuple']
    shutil = ops['shutil']
    if not shutil.which("clawpatch"):
        raise SafetyError("Clawpatch is not installed or is not available on PATH.")
    text = _must_run(["clawpatch", "--version"], cwd=repo, timeout=30).strip()
    if _version_tuple(text) < MINIMUM_CLAWPATCH_VERSION:
        raise SafetyError("Clawpatch 0.7.2 or newer is required.")
    return text


def _impl_version_tuple(
    ops: Any,
    text: str,
) -> tuple[int, int, int]:
    SafetyError = ops['SafetyError']
    re = ops['re']
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    if not match:
        raise SafetyError(f"Could not read the installed Clawpatch version from: {text.strip()!r}")
    return tuple(int(value) for value in match.groups())


def _impl_run_clawpatch(
    ops: Any,
    repo: Path,
    argv: list[str],
    *,
    env: dict[str, str],
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    _child_timeout_seconds = ops['_child_timeout_seconds']
    _require_no_process = ops['_require_no_process']
    _run = ops['_run']
    _require_no_process(repo)
    resolved_timeout = _child_timeout_seconds(env) if timeout is None else timeout
    return _run(
        argv,
        cwd=repo,
        timeout=resolved_timeout,
        env=env,
        kill_process_group=True,
    )


def _impl_must_clawpatch(
    ops: Any,
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
    SafetyError = ops['SafetyError']
    _ClawpatchCommandFailure = ops['_ClawpatchCommandFailure']
    _MissingFinding = ops['_MissingFinding']
    _child_timeout_seconds = ops['_child_timeout_seconds']
    _clawpatch_command_phase = ops['_clawpatch_command_phase']
    _run_clawpatch = ops['_run_clawpatch']
    _source_paths = ops['_source_paths']
    classify_clawpatch_failure = ops['classify_clawpatch_failure']
    redact_text = ops['redact_text']
    shlex = ops['shlex']
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
    stdout = redact_text(output)[-4000:]
    stderr = redact_text(result.stderr or "")[-4000:]
    raise _ClawpatchCommandFailure(
        f"phase: Clawpatch command\ncommand: {shlex.join(argv)}\nfinding ID: "
        f"{finding_id or 'N/A'}\nexit code: {result.returncode}\n"
        f"failed requirement: {watchdog}; this command is not retried\n"
        f"changed source paths: {_source_paths(repo)}\n"
        f"stdout:\n{stdout}\nstderr:\n{stderr}",
        failure=classify_clawpatch_failure(command_phase, result.returncode),
    )


def _impl_clawpatch_command_phase(
    ops: Any,
    argv: list[str],
) -> str:
    command = argv[1] if len(argv) > 1 else "clawpatch"
    if command == "clean-locks":
        return "lock-cleanup"
    if command == "review" and "--dry-run" in argv:
        return "review-verification"
    if command == "next":
        return "queue"
    return command


def _impl_json_clawpatch(
    ops: Any,
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
    _must_clawpatch = ops['_must_clawpatch']
    _parse_json_output = ops['_parse_json_output']
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


def _impl_run_project_gates(
    ops: Any,
    repo: Path,
    *,
    finding_id: str,
    required: bool = True,
) -> list[dict[str, Any]]:
    CommandRunner = ops['CommandRunner']
    GateFailure = ops['GateFailure']
    PROJECT_DIR = ops['PROJECT_DIR']
    PurePosixPath = ops['PurePosixPath']
    PureWindowsPath = ops['PureWindowsPath']
    SafetyError = ops['SafetyError']
    _source_paths = ops['_source_paths']
    shlex = ops['shlex']
    tomllib = ops['tomllib']
    if not required:
        return []
    config_path = repo / PROJECT_DIR / "config.toml"
    if not config_path.is_file():
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
        executable = argv[0]
        if (
            executable != PurePosixPath(executable).name
            or executable != PureWindowsPath(executable).name
        ):
            raise SafetyError(
                f"Validation gate {gate_id!r} uses an executable path; "
                "only bare program names are allowed."
            )
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


def _impl_validate_attempt_paths_syntax(
    ops: Any,
    paths: list[str],
) -> None:
    PurePosixPath = ops['PurePosixPath']
    PureWindowsPath = ops['PureWindowsPath']
    SafetyError = ops['SafetyError']
    invalid = []
    for path in paths:
        posix = PurePosixPath(path)
        windows = PureWindowsPath(path)
        if (
            not path
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or ".." in posix.parts
            or ".." in windows.parts
            or bool(windows.parts)
            and windows.parts[0].casefold() == ".clawpatch"
        ):
            invalid.append(path)
    if invalid:
        raise SafetyError(
            "Clawpatch patch attempt contains unsafe or state-only paths: " + ", ".join(invalid)
        )


def _impl_revalidation_payload(
    ops: Any,
    repo: Path,
    finding_id: str,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    phase: str = "revalidate",
    current: int | str = "?",
    total: int | str = "?",
) -> tuple[list[str], dict[str, Any], str]:
    SafetyError = ops['SafetyError']
    _json_clawpatch = ops['_json_clawpatch']
    _source_paths = ops['_source_paths']
    json = ops['json']
    shlex = ops['shlex']
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


def _impl_revalidate(
    ops: Any,
    repo: Path,
    finding_id: str,
    *,
    env: dict[str, str],
    expected_paths: list[str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
    _ClawpatchCommandFailure = ops['_ClawpatchCommandFailure']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _revalidation_payload = ops['_revalidation_payload']
    _source_paths = ops['_source_paths']
    _source_state_fingerprint = ops['_source_state_fingerprint']
    classify_clawpatch_failure = ops['classify_clawpatch_failure']
    json = ops['json']
    shlex = ops['shlex']
    if sorted(expected_paths) != _source_paths(repo):
        raise SafetyError(
            "Revalidation source paths no longer match the validated Clawpatch patch attempt."
        )
    before = _source_state_fingerprint(repo)
    argv = ["clawpatch", "revalidate", "--finding", finding_id, "--json"]

    def guarded_revalidation(
        attempt_env: dict[str, str],
        *,
        phase: str = "revalidate",
    ) -> tuple[list[str], dict[str, Any], str]:
        try:
            attempt = _revalidation_payload(
                repo,
                finding_id,
                env=attempt_env,
                progress=progress,
                phase=phase,
                current=current,
                total=total,
            )
        except SafetyError as exc:
            after = _source_state_fingerprint(repo)
            if after != before:
                raise _UnresolvedFinding(
                    f"{exc}\nfailed requirement: failed revalidation source progress must be "
                    "preserved and retried on the same finding",
                    finding_id=finding_id,
                    outcome="revalidation-command-failed-with-source-progress",
                    failure=classify_clawpatch_failure("revalidation", 23),
                ) from exc
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
            raise
        attempt_argv, _attempt_payload, _attempt_outcome = attempt
        if _source_state_fingerprint(repo) != before:
            raise _UnresolvedFinding(
                f"phase: revalidation\ncommand: {shlex.join(attempt_argv)}\n"
                f"finding ID: {finding_id}\n"
                "exit code: 0\nfailed requirement: revalidation must not alter source\n"
                f"changed source paths: {_source_paths(repo)}",
                finding_id=finding_id,
                outcome="revalidation-mutated-source",
                failure=classify_clawpatch_failure("revalidation", 23),
            )
        return attempt

    argv, payload, outcome = guarded_revalidation(env)
    if outcome in {"open", "uncertain"} and env.get("CLAWPATCH_CODEX_SANDBOX") in {
        None,
        "read-only",
    }:
        initial_outcome = outcome
        escalated_env = dict(env)
        escalated_env["CLAWPATCH_CODEX_SANDBOX"] = "workspace-write"
        argv, escalated, escalated_outcome = guarded_revalidation(
            escalated_env,
            phase="revalidate-escalated",
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
            argv, host_payload, host_outcome = guarded_revalidation(
                host_env,
                phase="revalidate-host",
            )
            payload = dict(host_payload)
            payload["managerooSandboxEscalated"] = True
            payload["managerooHostSandboxBypassed"] = True
            payload["managerooInitialOutcome"] = initial_outcome
            payload["managerooWorkspaceWriteOutcome"] = workspace_write_outcome
            outcome = host_outcome
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


def _impl_map_repository(
    ops: Any,
    repo: Path,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    _json_clawpatch = ops['_json_clawpatch']
    _required_int = ops['_required_int']
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
