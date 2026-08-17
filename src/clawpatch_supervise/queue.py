from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SafetyError


CLAWPATCH_CHILD_WATCHDOG_SECONDS = 900


@dataclass(frozen=True)
class QueueResult:
    processed: int
    open_findings: int
    uncertain_findings: int

    @classmethod
    def from_report(cls, report: dict[str, object]) -> QueueResult:
        values = (
            report.get("finding_count", 0),
            report.get("open_findings", 0),
            report.get("uncertain_findings", 0),
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
            raise SafetyError("The supervisor received invalid final queue counts.")
        return cls(*values)

    @property
    def complete(self) -> bool:
        return self.open_findings == 0 and self.uncertain_findings == 0


# Release-engine component implementations. The compatibility facade remains in clawpatch_release.

def _impl_next_finding(
    ops: Any,
    repo: Path,
    *,
    env: dict[str, str],
    status: str = "open",
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> tuple[str | None, dict[str, Any]]:
    SafetyError = ops['SafetyError']
    _FINDING_ID = ops['_FINDING_ID']
    _json_clawpatch = ops['_json_clawpatch']
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


def _impl_show_finding(
    ops: Any,
    repo: Path,
    finding_id: str,
    *,
    env: dict[str, str],
    required_status: str | None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
    _json_clawpatch = ops['_json_clawpatch']
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


def _impl_finding_from_fix_argv(
    ops: Any,
    argv: list[str],
) -> str:
    SafetyError = ops['SafetyError']
    _FINDING_ID = ops['_FINDING_ID']
    if len(argv) < 2 or argv[1] != "fix":
        raise SafetyError("Expected Clawpatch to direct a fix command.")
    try:
        value = argv[argv.index("--finding") + 1]
    except (ValueError, IndexError) as exc:
        raise SafetyError("Clawpatch fix command did not name a finding.") from exc
    if not _FINDING_ID.fullmatch(value):
        raise SafetyError(f"Clawpatch fix command returned an invalid finding ID: {value!r}")
    return value


def _impl_with_json(
    ops: Any,
    argv: list[str],
) -> list[str]:
    return list(argv) if "--json" in argv else [*argv, "--json"]


def _impl_fix_command(
    ops: Any,
    repo: Path, argv: list[str], *, env: dict[str, str] | None = None
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _finding_from_fix_argv = ops['_finding_from_fix_argv']
    _parse_json_output = ops['_parse_json_output']
    _run_clawpatch = ops['_run_clawpatch']
    _source_paths = ops['_source_paths']
    _with_json = ops['_with_json']
    classify_clawpatch_failure = ops['classify_clawpatch_failure']
    os = ops['os']
    shlex = ops['shlex']
    finding_id = _finding_from_fix_argv(argv)
    command = _with_json(argv)
    result = _run_clawpatch(
        repo,
        command,
        env=env if env is not None else dict(os.environ),
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


def _impl_patch_attempt_from_show(
    ops: Any,
    show_payload: dict[str, Any], patch_attempt_id: str, finding_id: str
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
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


def _impl_validate_attempt_paths(
    ops: Any,
    repo: Path, files: list[str],
) -> None:
    SafetyError = ops['SafetyError']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    _validate_attempt_paths_syntax(files)
    current = _source_paths(repo)
    if sorted(files) != current:
        raise SafetyError(
            "Changed source paths do not exactly match the current Clawpatch patch attempt; "
            f"attempt={sorted(files)!r}, current={current!r}."
        )


def _impl_process_finding_until_fixed(
    ops: Any,
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
    CLAWPATCH_ZERO_SOURCE_RETRY_LIMIT = ops['CLAWPATCH_ZERO_SOURCE_RETRY_LIMIT']
    ClawpatchFailureKind = ops['ClawpatchFailureKind']
    RepairAction = ops['RepairAction']
    SafetyError = ops['SafetyError']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _clear_release_progress = ops['_clear_release_progress']
    _complete_fixed_finding = ops['_complete_fixed_finding']
    _discard_checkpoint_owned_source = ops['_discard_checkpoint_owned_source']
    _execute_fix = ops['_execute_fix']
    _git_text = ops['_git_text']
    _paths_between = ops['_paths_between']
    _revalidate = ops['_revalidate']
    _run_project_gates = ops['_run_project_gates']
    _save_partial_iteration = ops['_save_partial_iteration']
    _source_paths = ops['_source_paths']
    _stop_finding_iteration = ops['_stop_finding_iteration']
    _verify_iteration_commit = ops['_verify_iteration_commit']
    _write_release_progress = ops['_write_release_progress']
    decide_repair_transition = ops['decide_repair_transition']
    failure_from_legacy_outcome = ops['failure_from_legacy_outcome']
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
        if revalidation_decision.action is RepairAction.STOP_TERMINAL:
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
            raise _UnresolvedFinding(
                "Clawpatch returned uncertain without an applied source repair; "
                "the finding remains stopped at its durable checkpoint.",
                finding_id=finding_id,
                outcome="uncertain-no-progress",
                repair_action=revalidation_decision.action,
            )
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


def _impl_resume_stopped_attempt(
    ops: Any,
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
    GateFailure = ops['GateFailure']
    RepairAction = ops['RepairAction']
    SafetyError = ops['SafetyError']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _attempt_base_preserves_owned_source = ops['_attempt_base_preserves_owned_source']
    _commit_attempt = ops['_commit_attempt']
    _git_text = ops['_git_text']
    _normalized_stopped_owned_paths = ops['_normalized_stopped_owned_paths']
    _owned_source_fingerprint = ops['_owned_source_fingerprint']
    _push_and_verify = ops['_push_and_verify']
    _revalidate = ops['_revalidate']
    _run_project_gates = ops['_run_project_gates']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths = ops['_validate_attempt_paths']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    _verify_iteration_commit = ops['_verify_iteration_commit']
    re = ops['re']
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


def _impl_execute_fix(
    ops: Any,
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
    SafetyError = ops['SafetyError']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _commit_attempt = ops['_commit_attempt']
    _fix_command = ops['_fix_command']
    _git_text = ops['_git_text']
    _patch_attempt_from_show = ops['_patch_attempt_from_show']
    _push_and_verify = ops['_push_and_verify']
    _require_no_process = ops['_require_no_process']
    _revalidate = ops['_revalidate']
    _run_project_gates = ops['_run_project_gates']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths = ops['_validate_attempt_paths']
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


def _impl_required_int(
    ops: Any,
    payload: dict[str, Any], field: str,
) -> int:
    SafetyError = ops['SafetyError']
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SafetyError(f"Clawpatch returned a missing or malformed {field!r} value.")
    return value


def _impl_review_probe(
    ops: Any,
    repo: Path,
    *,
    env: dict[str, str],
    review_limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
    current: int | str = "?",
    total: int | str = "?",
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
    _json_clawpatch = ops['_json_clawpatch']
    _required_int = ops['_required_int']
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


def _impl_review_completion(
    ops: Any,
    repo: Path,
    *,
    env: dict[str, str],
    review_limit: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
    _required_int = ops['_required_int']
    _review_probe = ops['_review_probe']
    payload = _review_probe(
        repo,
        env=env,
        review_limit=review_limit,
        progress=progress,
    )
    if _required_int(payload, "wouldReview") != 0:
        raise SafetyError("Clawpatch still has pending or errored features requiring review.")
    return payload


def _impl_review_all_features(
    ops: Any,
    repo: Path,
    *,
    env: dict[str, str],
    mapped_features: int,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
    _json_clawpatch = ops['_json_clawpatch']
    _required_int = ops['_required_int']
    _review_probe = ops['_review_probe']
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


def _impl_resolve_uncertain_findings(
    ops: Any,
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
    SafetyError = ops['SafetyError']
    _FINDING_ID = ops['_FINDING_ID']
    _next_finding = ops['_next_finding']
    _revalidate = ops['_revalidate']
    _run_project_gates = ops['_run_project_gates']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
    if (
        not isinstance(uncertain_total, int)
        or isinstance(uncertain_total, bool)
        or uncertain_total < 0
    ):
        raise SafetyError("Clawpatch returned an invalid uncertain-finding count.")
    if finding_ids is not None and (
        len(finding_ids) != uncertain_total
        or len(set(finding_ids)) != len(finding_ids)
        or any(not _FINDING_ID.fullmatch(finding_id) for finding_id in finding_ids)
    ):
        raise SafetyError("Clawpatch uncertain report returned invalid finding IDs.")
    if _source_paths(repo):
        raise SafetyError("Uncommitted source changes block uncertain-finding recovery.")
    recovered: list[dict[str, Any]] = []
    reopened: list[str] = []
    for index in range(1, uncertain_total + 1):
        displayed = current_offset + index
        display_total = current_offset + uncertain_total
        if finding_ids is None:
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
                    "Clawpatch uncertain report count changed before every finding could be "
                    "revalidated."
                )
        else:
            finding_id = finding_ids[index - 1]
            queue = {"finding": {"id": finding_id, "status": "uncertain"}}
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
        elif validation.get("outcome") == "uncertain" and retain_uncertain:
            record["retained_uncertain"] = True
            recovered.append(record)
        else:
            raise SafetyError(
                f"Uncertain-finding recovery returned an unsupported outcome for {finding_id}."
            )
        if _source_paths(repo):
            raise SafetyError("Uncertain-finding revalidation unexpectedly changed source files.")
    if finding_ids is None:
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


def _impl_release_sweep_locked(
    ops: Any,
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
    ClawpatchStop = ops['ClawpatchStop']
    DirtySourcePolicy = ops['DirtySourcePolicy']
    LIFECYCLE = ops['LIFECYCLE']
    RepairAction = ops['RepairAction']
    RepositoryBusyError = ops['RepositoryBusyError']
    SafetyError = ops['SafetyError']
    _MissingFinding = ops['_MissingFinding']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _checkpoint_can_follow_supervisor_upgrade = ops['_checkpoint_can_follow_supervisor_upgrade']
    _checkpoint_completed_commit = ops['_checkpoint_completed_commit']
    _checkpoint_cross_finding_applied_attempt = ops['_checkpoint_cross_finding_applied_attempt']
    _checkpoint_false_positive_without_source = ops['_checkpoint_false_positive_without_source']
    _checkpoint_fixed_without_source = ops['_checkpoint_fixed_without_source']
    _checkpoint_later_applied_attempt = ops['_checkpoint_later_applied_attempt']
    _checkpoint_proves_exact_source = ops['_checkpoint_proves_exact_source']
    _checkpoint_same_finding_later_applied_attempt = ops['_checkpoint_same_finding_later_applied_attempt']
    _checkpoint_unapplied_attempt = ops['_checkpoint_unapplied_attempt']
    _clawpatch_version = ops['_clawpatch_version']
    _clean_descendant_retires_verified_checkpoint = ops['_clean_descendant_retires_verified_checkpoint']
    _clear_release_progress = ops['_clear_release_progress']
    _commit_ambiguous_checkpoint_source_baseline = ops['_commit_ambiguous_checkpoint_source_baseline']
    _commit_preexisting_source_baseline = ops['_commit_preexisting_source_baseline']
    _current_input_baseline_commit = ops['_current_input_baseline_commit']
    _discard_checkpoint_owned_source = ops['_discard_checkpoint_owned_source']
    _final_closure = ops['_final_closure']
    _git_root = ops['_git_root']
    _git_text = ops['_git_text']
    _json_clawpatch = ops['_json_clawpatch']
    _load_release_progress = ops['_load_release_progress']
    _map_repository = ops['_map_repository']
    _migrate_legacy_external_progress = ops['_migrate_legacy_external_progress']
    _must_run = ops['_must_run']
    _next_finding = ops['_next_finding']
    _prepare_fresh_release = ops['_prepare_fresh_release']
    _process_finding_until_fixed = ops['_process_finding_until_fixed']
    _push_and_verify = ops['_push_and_verify']
    _rebuilt_generation_owns_checkpoint_source = ops['_rebuilt_generation_owns_checkpoint_source']
    _rebuilt_generation_supersedes_empty_checkpoint = ops['_rebuilt_generation_supersedes_empty_checkpoint']
    _recover_interrupted_source_clean_fix = ops['_recover_interrupted_source_clean_fix']
    _release_clawpatch_env = ops['_release_clawpatch_env']
    _release_state_root = ops['_release_state_root']
    _release_sweep_locked = ops['_release_sweep_locked']
    _require_no_process = ops['_require_no_process']
    _require_synchronized_remote_branch = ops['_require_synchronized_remote_branch']
    _required_int = ops['_required_int']
    _resume_stopped_attempt = ops['_resume_stopped_attempt']
    _review_all_features = ops['_review_all_features']
    _run_project_gates = ops['_run_project_gates']
    _save_partial_iteration = ops['_save_partial_iteration']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
    _stop_finding_iteration = ops['_stop_finding_iteration']
    _write_release_progress = ops['_write_release_progress']
    datetime = ops['datetime']
    timezone = ops['timezone']
    write_completion_proof = ops['write_completion_proof']
    root = _git_root(repo)
    if integration_mode not in {"manageroo", "external"}:
        raise SafetyError("integration_mode must be one of: manageroo, external.")
    require_project_gates = integration_mode == "manageroo"
    dirty_policy = DirtySourcePolicy(adopt_dirty=adopt_dirty)
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
    preexisting_baseline_commit = (
        _preexisting_baseline_commit or _current_input_baseline_commit(root)
    )
    generation_result_start = len(report["results"])
    if not apply:
        report["planned_branch"] = branch
        return report

    _require_no_process(root)
    env = _release_clawpatch_env(
        trusted_host_codex_sandbox_bypass=trusted_host_codex_sandbox_bypass,
        allow_sandbox_bypass_fallback=(integration_mode == "external"),
        child_timeout_seconds=child_timeout_seconds,
        deadline_monotonic=deadline_monotonic,
        child_env_overrides=child_env_overrides,
        supervisor_path_override=supervisor_path_override,
    )
    if integration_mode == "external":
        _migrate_legacy_external_progress(root, state_root=state_root)
    if (
        fresh
        and integration_mode == "external"
        and _source_paths(root)
        and _load_release_progress(root, state_root=state_root) is None
    ):
        dirty_policy.require_authorized(
            root,
            _source_paths(root),
            context="Automatic fresh review",
        )
        fresh_baseline = _commit_preexisting_source_baseline(
            root,
            _source_paths(root),
            state_root=state_root,
        )
        report["preexisting_source_baseline"] = fresh_baseline
        preexisting_baseline_commit = str(fresh_baseline["baseline_commit"])
        if progress is not None:
            progress(
                {
                    "phase": "baseline-commit",
                    "current": "?",
                    "total": "?",
                    "command": (
                        "commit current source as the ClawPatch input baseline; "
                        "keep visible file content; rebuild ClawPatch queue"
                    ),
                    "attempt": 1,
                    "max_attempts": 1,
                    "owned_paths": list(fresh_baseline["paths"]),
                    "commit": fresh_baseline["baseline_commit"],
                    "baseline_ref": fresh_baseline["baseline_ref"],
                    "receipt": fresh_baseline["receipt"],
                }
            )
    if (
        fresh
        and integration_mode == "external"
        and wait_on_preserved_source
        and _source_paths(root)
    ):
        raise RepositoryBusyError(
            "Automatic fresh review found preserved project source changes; waiting without "
            "discarding them."
        )
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
            retired_branch = str(durable_progress["branch"])
            retired_finding = str(durable_progress["finding_id"])
            retired_paths = list(durable_progress["owned_paths"])
            _clear_release_progress(root, state_root=state_root)
            report["retired_branch_progress"] = {
                "branch": retired_branch,
                "current_branch": current_branch,
                "finding_id": retired_finding,
                "owned_paths": retired_paths,
            }
            if progress is not None:
                progress(
                    {
                        "phase": "reset-recovery",
                        "current": "?",
                        "total": "?",
                        "finding_id": retired_finding,
                        "command": (
                            "retire interrupted progress from a different branch; "
                            "continue on the current branch without changing source"
                        ),
                        "attempt": 1,
                        "max_attempts": 1,
                        "owned_paths": retired_paths,
                        "retired_branch": retired_branch,
                        "current_branch": current_branch,
                    }
                )
            durable_progress = None
        if (
            durable_progress is not None
            and durable_progress["phase"] == "finalized"
            and not preexisting_source
        ):
            completed_commit = _checkpoint_completed_commit(root, durable_progress)
            if completed_commit:
                completed_finding = str(durable_progress["finding_id"])
                if progress is not None:
                    progress(
                        {
                            "phase": "reset-recovery",
                            "current": "?",
                            "total": "?",
                            "finding_id": completed_finding,
                            "command": (
                                "retire completed checkpoint after interrupted push; "
                                "keep its committed repair and reconcile the remote"
                            ),
                            "attempt": 1,
                            "max_attempts": 1,
                            "owned_paths": [],
                            "commit": completed_commit,
                        }
                    )
                _clear_release_progress(root, state_root=state_root)
                report["finalized_checkpoint_recovery"] = {
                    "finding_id": completed_finding,
                    "commit": completed_commit,
                }
                durable_progress = None
        if durable_progress is not None and _rebuilt_generation_supersedes_empty_checkpoint(
            root, durable_progress
        ):
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
            and integration_mode == "external"
            and _clean_descendant_retires_verified_checkpoint(root, durable_progress)
        ):
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
                            "preserve unrelated source and ClawPatch queue"
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
        if (
            durable_progress is not None
            and preexisting_source
            and (
                sorted(str(path) for path in durable_progress["owned_paths"])
                != preexisting_source
                or not durable_progress.get("owned_source_fingerprint")
                or not _checkpoint_proves_exact_source(
                    root,
                    durable_progress,
                    preexisting_source,
                )
            )
        ):
            later_applied = _checkpoint_cross_finding_applied_attempt(
                root,
                durable_progress,
                env=env,
                progress=progress,
            )
            if (
                later_applied is None
                and durable_progress["head_before"] != head_before
                and durable_progress.get("owned_source_fingerprint")
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
                later_applied = _checkpoint_same_finding_later_applied_attempt(
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
                    finding_id=str(later_applied["finding_id"]),
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
            and (
                integration_mode == "external"
                or (
                    durable_progress["head_before"] != head_before
                    and durable_progress.get("owned_source_fingerprint")
                )
            )
            and not _checkpoint_proves_exact_source(
                root,
                durable_progress,
                preexisting_source,
            )
        ):
            dirty_policy.require_authorized(
                root,
                preexisting_source,
                context="Ambiguous checkpoint recovery",
            )
            ambiguous_baseline = _commit_ambiguous_checkpoint_source_baseline(
                root,
                durable_progress,
                preexisting_source,
                state_root=state_root,
            )
            if ambiguous_baseline is not None:
                report["ambiguous_checkpoint_baseline"] = ambiguous_baseline
                preexisting_baseline_commit = str(ambiguous_baseline["baseline_commit"])
                if progress is not None:
                    progress(
                        {
                            "phase": "baseline-commit",
                            "current": "?",
                            "total": "?",
                            "finding_id": ambiguous_baseline["finding_id"],
                            "command": (
                                "commit ambiguous current source as the ClawPatch input baseline; "
                                "retire stale checkpoint; continue ClawPatch queue"
                            ),
                            "attempt": 1,
                            "max_attempts": 1,
                            "owned_paths": list(ambiguous_baseline["paths"]),
                            "commit": ambiguous_baseline["baseline_commit"],
                            "baseline_ref": ambiguous_baseline["baseline_ref"],
                            "receipt": ambiguous_baseline["receipt"],
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
                    conflict = (
                        "Interrupted Clawpatch release progress cannot prove exact "
                        "checkpoint-owned source content; preserving ambiguous changes for "
                        "operator review: " + ", ".join(preexisting_source)
                    )
                    if integration_mode == "external":
                        raise RepositoryBusyError(conflict + "; waiting without discarding them.")
                    raise SafetyError(conflict)
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
                        conflict = (
                            "Interrupted Clawpatch release progress no longer owns the exact "
                            "current source paths"
                        )
                        if integration_mode == "external":
                            raise RepositoryBusyError(
                                conflict + "; waiting without discarding them."
                            )
                        raise SafetyError(conflict + ".")
                    if not _checkpoint_proves_exact_source(
                        root,
                        durable_progress,
                        preexisting_source,
                    ):
                        conflict = (
                            "Interrupted Clawpatch release progress cannot prove exact "
                            "checkpoint-owned source content; preserving ambiguous changes for "
                            "operator review: " + ", ".join(preexisting_source)
                        )
                        if integration_mode == "external":
                            raise RepositoryBusyError(
                                conflict + "; waiting without discarding them."
                            )
                        raise SafetyError(conflict)
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
    if (
        integration_mode == "external"
        and durable_progress is not None
        and preexisting_source
        and (
            sorted(str(path) for path in durable_progress["owned_paths"]) != preexisting_source
            or not durable_progress.get("owned_source_fingerprint")
            or not _checkpoint_proves_exact_source(root, durable_progress, preexisting_source)
        )
    ):
        if sorted(str(path) for path in durable_progress["owned_paths"]) != preexisting_source:
            conflict = (
                "Interrupted Clawpatch release progress no longer owns the exact current "
                "source paths"
            )
        else:
            conflict = (
                "Interrupted Clawpatch release progress cannot prove exact checkpoint-owned "
                "source content"
            )
        raise RepositoryBusyError(conflict + "; waiting without discarding them.")
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
        message = (
            "Clawpatch release sweep found preserved pre-existing source changes: "
            + ", ".join(preexisting_source)
        )
        if integration_mode == "external":
            dirty_policy.require_authorized(
                root,
                preexisting_source,
                context="ClawPatch release sweep",
            )
            preexisting_baseline = _commit_preexisting_source_baseline(
                root,
                preexisting_source,
                state_root=state_root,
            )
            report["preexisting_source_baseline"] = preexisting_baseline
            preexisting_baseline_commit = str(preexisting_baseline["baseline_commit"])
            if progress is not None:
                progress(
                    {
                        "phase": "baseline-commit",
                        "current": "?",
                        "total": "?",
                        "command": (
                            "commit current source as the ClawPatch input baseline; "
                            "keep visible file content; continue ClawPatch queue"
                        ),
                        "attempt": 1,
                        "max_attempts": 1,
                        "owned_paths": list(preexisting_baseline["paths"]),
                        "commit": preexisting_baseline["baseline_commit"],
                        "baseline_ref": preexisting_baseline["baseline_ref"],
                        "receipt": preexisting_baseline["receipt"],
                    }
                )
            preexisting_source = []
        else:
            raise SafetyError(message)
    if durable_progress is not None and branch not in {"auto", "current", current_branch}:
        raise SafetyError(
            "Cannot create a different branch while resuming interrupted Clawpatch release progress."
        )
    if push_mode != "none":
        _require_synchronized_remote_branch(
            root,
            current_branch,
            progress=progress,
            preserve_local_on_conflict=bool(preexisting_baseline_commit),
        )
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
                env=env,
                progress=progress,
            )
            if later_applied is not None:
                recovered_paths = list(later_applied["owned_paths"])
                durable_progress = _write_release_progress(
                    root,
                    finding_id=str(later_applied["finding_id"]),
                    branch=str(durable_progress["branch"]),
                    head_before=_git_text(root, ["git", "rev-parse", "HEAD"]),
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
                "configured Manageroo repository gates"
                if require_project_gates
                else "ClawPatch-owned finding validation (full repository gates are not run)"
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
        refresh_retained_uncertain=advance_uncertain,
    )
    if (
        preexisting_baseline_commit
        and push_mode == "each"
        and not bool(closure.get("pushed", pushed))
    ):
        if progress is not None:
            progress(
                {
                    "phase": "push",
                    "current": current_finding,
                    "total": total_findings,
                    "command": f"git push origin {selected_branch}",
                    "detail": "publish the verified input baseline after queue completion",
                    "attempt": 1,
                    "max_attempts": 1,
                    "commit": preexisting_baseline_commit,
                }
            )
        _push_and_verify(root, selected_branch, first=not pushed)
        pushed = True
        closure["pushed"] = True
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
            supervisor_path_override=supervisor_path_override,
            advance_uncertain=advance_uncertain,
            adopt_dirty=adopt_dirty,
            deadline_monotonic=deadline_monotonic,
            _fixed_point_generation=_fixed_point_generation + 1,
            _fixed_point_seen_trees=(*_fixed_point_seen_trees, generation_tree),
            _prior_results=tuple(report["results"]),
            _prior_continuations=tuple(report["continuations"]),
            _prior_false_positives=tuple(report["false_positives"]),
            _prior_review_generations=tuple(report["review_generations"]),
            _already_pushed=bool(closure.get("pushed", pushed)),
            _preexisting_baseline_commit=preexisting_baseline_commit,
        )
    final_head = _git_text(root, ["git", "rev-parse", "HEAD"])
    final_uncertain_count = _required_int(
        closure.get("uncertain_report", {"total": 0}), "total"
    )
    proof_path = write_completion_proof(
        state_root=state_root,
        repo=root,
        branch=selected_branch,
        git_head=final_head,
        clawpatch_version=version,
        completed_findings=report["results"],
        continuation_attempts=report["continuations"],
        false_positives=report["false_positives"],
        review_generations=report["review_generations"],
        final_closure=closure,
        open_findings=0,
        uncertain_findings=final_uncertain_count,
        allow_uncertain=advance_uncertain,
    )
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
