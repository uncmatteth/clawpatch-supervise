from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .checkpoint import CheckpointStore
from .clawpatch_protocol import RepairAction
from .util import utc_now
from .validation import CompletionValidation


def write_completion_proof(
    *,
    state_root: Path,
    repo: Path,
    branch: str,
    git_head: str,
    clawpatch_version: str,
    completed_findings: list[dict[str, Any]],
    continuation_attempts: list[dict[str, Any]],
    false_positives: list[dict[str, Any]],
    review_generations: list[dict[str, Any]],
    final_closure: dict[str, Any],
    open_findings: int,
    uncertain_findings: int,
    allow_uncertain: bool = False,
) -> Path:
    completion = CompletionValidation.require_complete(
        open_findings=open_findings,
        uncertain_findings=uncertain_findings,
        allow_uncertain=allow_uncertain,
    )
    payload = {
        "status": completion.status,
        "completed_at": utc_now(),
        "repo": str(repo),
        "branch": branch,
        "git_head": git_head,
        "clawpatch_version": clawpatch_version,
        "open_findings": completion.open_findings,
        "uncertain_findings": completion.uncertain_findings,
        "completed_findings": completed_findings,
        "continuation_attempts": continuation_attempts,
        "false_positives": false_positives,
        "review_generations": review_generations,
        "final_closure": final_closure,
    }
    store = CheckpointStore(state_root)
    store.write_proof(payload)
    return store.proof_path


# Release-engine component implementations. The compatibility facade remains in clawpatch_release.

def _impl_prepare_fresh_release(
    ops: Any,
    repo: Path,
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    state_root: Path | None = None,
) -> None:
    PROJECT_DIR = ops['PROJECT_DIR']
    Path = ops['Path']
    SafetyError = ops['SafetyError']
    _clear_release_progress = ops['_clear_release_progress']
    _committed_clawpatch_config = ops['_committed_clawpatch_config']
    _exclude_gitlinks_from_clawpatch_config = ops['_exclude_gitlinks_from_clawpatch_config']
    _git_text = ops['_git_text']
    _json_clawpatch = ops['_json_clawpatch']
    _recover_checkpoint_temporary_commit = ops['_recover_checkpoint_temporary_commit']
    _require_no_process = ops['_require_no_process']
    _source_paths = ops['_source_paths']
    shutil = ops['shutil']
    """Replace Clawpatch state transactionally while preserving project configuration."""
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
    backup_text = _git_text(
        repo,
        ["git", "rev-parse", "--git-path", "clawpatch-supervise-fresh-queue-backup"],
    )
    backup_path = Path(backup_text)
    if not backup_path.is_absolute():
        backup_path = repo / backup_path
    backup_path = backup_path.absolute()
    if backup_path.is_symlink():
        raise SafetyError("The fresh Clawpatch queue backup path cannot be a symlink.")
    if backup_path.exists():
        if clawpatch_state_root.exists():
            raise SafetyError(
                "Both the active Clawpatch queue and a retained fresh-reset backup exist. "
                f"The supervisor preserved both for inspection: {backup_path}"
            )
        if not backup_path.is_dir():
            raise SafetyError("The retained fresh Clawpatch queue backup is not a directory.")
        backup_path.replace(clawpatch_state_root)
    if clawpatch_state_root.exists():
        if not clawpatch_state_root.is_dir():
            raise SafetyError("The .clawpatch state path is not a directory.")
        clawpatch_state_root.replace(backup_path)
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
    try:
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
    except BaseException as exc:
        try:
            if clawpatch_state_root.exists():
                if clawpatch_state_root.is_symlink() or not clawpatch_state_root.is_dir():
                    raise SafetyError(
                        "Fresh initialization left an unsafe .clawpatch path; the old queue remains at "
                        f"{backup_path}."
                    )
                shutil.rmtree(clawpatch_state_root)
            if backup_path.exists():
                backup_path.replace(clawpatch_state_root)
        except (OSError, SafetyError) as restore_exc:
            raise SafetyError(
                "Fresh Clawpatch initialization failed and the supervisor could not restore the old "
                f"queue automatically. The backup remains at {backup_path}: {restore_exc}"
            ) from exc
        raise
    if backup_path.exists():
        shutil.rmtree(backup_path)
    _clear_release_progress(repo, state_root=state_root)
    proof_root = state_root if state_root is not None else repo / PROJECT_DIR / "cache"
    (proof_root / "clawpatch-release-proof.json").unlink(missing_ok=True)
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


def _impl_save_partial_iteration(
    ops: Any,
    repo: Path,
    *,
    finding_id: str,
    branch: str,
    original_head: str,
    temporary_commit: str,
    seen_states: set[str],
    state_root: Path,
) -> tuple[str, list[str], str]:
    RepairAction = ops['RepairAction']
    SafetyError = ops['SafetyError']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _commit_without_local_hooks = ops['_commit_without_local_hooks']
    _git_text = ops['_git_text']
    _require_branch = ops['_require_branch']
    _source_paths = ops['_source_paths']
    _stage_current_source = ops['_stage_current_source']
    _verify_iteration_commit = ops['_verify_iteration_commit']
    _write_release_progress = ops['_write_release_progress']
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


def _impl_stop_finding_iteration(
    ops: Any,
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
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    _verify_iteration_commit = ops['_verify_iteration_commit']
    _write_release_progress = ops['_write_release_progress']
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


def _impl_complete_fixed_finding(
    ops: Any,
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
    SafetyError = ops['SafetyError']
    _clear_release_progress = ops['_clear_release_progress']
    _finalize_finding_commit = ops['_finalize_finding_commit']
    _git_text = ops['_git_text']
    _paths_between = ops['_paths_between']
    _push_and_verify = ops['_push_and_verify']
    _source_paths = ops['_source_paths']
    _stop_finding_iteration = ops['_stop_finding_iteration']
    _write_release_progress = ops['_write_release_progress']
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


def _impl_restore_committed_clawpatch_state(
    ops: Any,
    repo: Path,
) -> None:
    SafetyError = ops['SafetyError']
    _must_run = ops['_must_run']
    _source_state_fingerprint = ops['_source_state_fingerprint']
    _status_paths = ops['_status_paths']
    shutil = ops['shutil']
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


def _impl_final_closure(
    ops: Any,
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
    SafetyError = ops['SafetyError']
    _json_clawpatch = ops['_json_clawpatch']
    _next_finding = ops['_next_finding']
    _process_finding_until_fixed = ops['_process_finding_until_fixed']
    _publish_final_state = ops['_publish_final_state']
    _push_and_verify = ops['_push_and_verify']
    _require_no_process = ops['_require_no_process']
    _required_int = ops['_required_int']
    _resolve_uncertain_findings = ops['_resolve_uncertain_findings']
    _review_completion = ops['_review_completion']
    _run_project_gates = ops['_run_project_gates']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
    _status_paths = ops['_status_paths']
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
    revalidated_uncertain: list[dict[str, Any]] = []
    if uncertain_total and (resolve_uncertain or refresh_retained_uncertain):
        current_offset = (
            current if isinstance(current, int) and not isinstance(current, bool) else 0
        )
        if refresh_retained_uncertain and not resolve_uncertain:
            finding_ids = [
                item.get("id") if isinstance(item, dict) else None
                for item in uncertain_items
            ]
            if any(not isinstance(finding_id, str) for finding_id in finding_ids):
                raise SafetyError("Final Clawpatch uncertain report has invalid finding IDs.")
            refreshed, reopened = _resolve_uncertain_findings(
                repo,
                env=env,
                uncertain_total=uncertain_total,
                require_project_gates=require_project_gates,
                progress=progress,
                current_offset=current_offset,
                finding_ids=finding_ids,
                retain_uncertain=True,
            )
            revalidated_uncertain = [
                record for record in refreshed if record.get("retained_uncertain")
            ]
            fixed_uncertain = [
                record for record in refreshed if not record.get("retained_uncertain")
            ]
        else:
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
        if resolve_uncertain and (
            uncertain_report.get("total") != 0 or uncertain_report.get("items") != []
        ):
            raise SafetyError("Final Clawpatch report still contains uncertain findings.")
        if refresh_retained_uncertain and not resolve_uncertain:
            remaining_items = uncertain_report.get("items")
            remaining_ids = [
                item.get("id") if isinstance(item, dict) else None
                for item in remaining_items
            ] if isinstance(remaining_items, list) else []
            retained_ids = [record["finding_id"] for record in revalidated_uncertain]
            if remaining_ids != retained_ids:
                raise SafetyError(
                    "Retained Clawpatch uncertain report changed unexpectedly after revalidation."
                )
        uncertain_total = _required_int(uncertain_report, "total")
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
        "revalidated_uncertain": revalidated_uncertain,
        "needs_fresh_review": needs_fresh_review,
    }
