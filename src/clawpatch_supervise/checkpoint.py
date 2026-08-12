from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import atomic_write_json


@dataclass(frozen=True)
class CheckpointStore:
    """Durable supervisor state kept outside the target worktree."""

    root: Path

    @property
    def progress_path(self) -> Path:
        return self.root / "clawpatch-release-progress.json"

    @property
    def proof_path(self) -> Path:
        return self.root / "clawpatch-release-proof.json"

    def write_progress(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.progress_path, payload)

    def clear_progress(self) -> None:
        self.progress_path.unlink(missing_ok=True)

    def write_proof(self, payload: dict[str, Any]) -> None:
        atomic_write_json(self.proof_path, payload)


# Release-engine component implementations. The compatibility facade remains in clawpatch_release.

def _impl_external_state_home(
    ops: Any,
) -> Path:
    Path = ops['Path']
    os = ops['os']
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "ClawPatchSupervise" / "state"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg_state_home) if xdg_state_home else Path.home() / ".local" / "state"
    return base / "clawpatch-supervise"


def _impl_legacy_external_state_homes(
    ops: Any,
) -> tuple[Path, ...]:
    Path = ops['Path']
    _external_state_home = ops['_external_state_home']
    os = ops['os']
    sys = ops['sys']
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


def _impl_repository_state_root(
    ops: Any,
    home: Path, repo: Path,
) -> Path:
    hashlib = ops['hashlib']
    os = ops['os']
    identity = hashlib.sha256(os.fsencode(str(repo.resolve()))).hexdigest()
    return home / "repositories" / identity


def _impl_release_state_root(
    ops: Any,
    repo: Path, *, integration_mode: str,
) -> Path:
    PROJECT_DIR = ops['PROJECT_DIR']
    SafetyError = ops['SafetyError']
    _external_state_home = ops['_external_state_home']
    _repository_state_root = ops['_repository_state_root']
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


def _impl_external_state_root(
    ops: Any,
    repo: Path,
) -> Path:
    _release_state_root = ops['_release_state_root']
    """Return the durable standalone state directory for a repository."""
    return _release_state_root(repo.resolve(), integration_mode="external")


def _impl_release_progress_path(
    ops: Any,
    repo: Path, *, state_root: Path | None = None,
) -> Path:
    PROJECT_DIR = ops['PROJECT_DIR']
    root = state_root if state_root is not None else repo / PROJECT_DIR / "cache"
    return root / "clawpatch-release-progress.json"


def _impl_write_release_progress(
    ops: Any,
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
    RELEASE_PROGRESS_VERSION = ops['RELEASE_PROGRESS_VERSION']
    RepairAction = ops['RepairAction']
    SafetyError = ops['SafetyError']
    _FINDING_ID = ops['_FINDING_ID']
    _owned_source_fingerprint = ops['_owned_source_fingerprint']
    _release_progress_path = ops['_release_progress_path']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    atomic_write_json = ops['atomic_write_json']
    re = ops['re']
    utc_now = ops['utc_now']
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


def _impl_load_release_progress(
    ops: Any,
    repo: Path,
    *,
    state_root: Path | None = None,
) -> dict[str, Any] | None:
    Path = ops['Path']
    RELEASE_PROGRESS_VERSION = ops['RELEASE_PROGRESS_VERSION']
    RepairAction = ops['RepairAction']
    SafetyError = ops['SafetyError']
    _FINDING_ID = ops['_FINDING_ID']
    _release_progress_path = ops['_release_progress_path']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    json = ops['json']
    re = ops['re']
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


def _impl_migrate_legacy_external_progress(
    ops: Any,
    repo: Path, *, state_root: Path,
) -> None:
    PROJECT_DIR = ops['PROJECT_DIR']
    RELEASE_PROGRESS_VERSION = ops['RELEASE_PROGRESS_VERSION']
    SafetyError = ops['SafetyError']
    _legacy_external_state_homes = ops['_legacy_external_state_homes']
    _legacy_owned_source_fingerprint = ops['_legacy_owned_source_fingerprint']
    _load_release_progress = ops['_load_release_progress']
    _owned_source_fingerprint = ops['_owned_source_fingerprint']
    _release_progress_path = ops['_release_progress_path']
    _repository_state_root = ops['_repository_state_root']
    atomic_write_json = ops['atomic_write_json']
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


def _impl_checkpoint_can_follow_supervisor_upgrade(
    ops: Any,
    repo: Path,
    progress: dict[str, Any],
) -> bool:
    _SUPERVISOR_UPGRADE_PATHS = ops['_SUPERVISOR_UPGRADE_PATHS']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _run = ops['_run']
    json = ops['json']
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


def _impl_checkpoint_completed_commit(
    ops: Any,
    repo: Path,
    progress: dict[str, Any],
) -> str:
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _run = ops['_run']
    if progress.get("phase") not in {"stopped", "finalized"}:
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


def _impl_clean_descendant_retires_verified_checkpoint(
    ops: Any,
    repo: Path,
    progress: dict[str, Any],
) -> bool:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _run = ops['_run']
    _source_paths = ops['_source_paths']
    _verify_iteration_commit = ops['_verify_iteration_commit']
    json = ops['json']
    """Retire only the recovery wrapper after its clean base has safely advanced.

    The ClawPatch finding remains in ``.clawpatch`` and is selected normally on
    the continuing run. This does not classify, skip, or otherwise advance it.
    """
    if progress.get("phase") != "stopped":
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
    if set(_source_paths(repo)).intersection(str(path) for path in owned_paths):
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


def _impl_checkpoint_unapplied_attempt(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    RepairAction = ops['RepairAction']
    _git_text = ops['_git_text']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
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


def _impl_recover_interrupted_source_clean_fix(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
    *,
    state_root: Path,
) -> dict[str, Any] | None:
    RepairAction = ops['RepairAction']
    _git_text = ops['_git_text']
    _run = ops['_run']
    _source_paths = ops['_source_paths']
    _write_release_progress = ops['_write_release_progress']
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


def _impl_attempt_base_preserves_owned_source(
    ops: Any,
    repo: Path,
    *,
    attempt_base: Any,
    current_head: str,
    owned_paths: list[str],
) -> bool:
    _run = ops['_run']
    re = ops['re']
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


def _impl_checkpoint_same_finding_later_applied_attempt(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
    *,
    inspected: dict[str, Any],
) -> dict[str, Any] | None:
    SafetyError = ops['SafetyError']
    _attempt_base_preserves_owned_source = ops['_attempt_base_preserves_owned_source']
    _git_text = ops['_git_text']
    _parse_checkpoint_time = ops['_parse_checkpoint_time']
    _run = ops['_run']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths = ops['_validate_attempt_paths']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    _verify_iteration_commit = ops['_verify_iteration_commit']
    datetime = ops['datetime']
    timezone = ops['timezone']
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


def _impl_checkpoint_cross_finding_applied_attempt(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str] | None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    SafetyError = ops['SafetyError']
    _FINDING_ID = ops['_FINDING_ID']
    _attempt_base_preserves_owned_source = ops['_attempt_base_preserves_owned_source']
    _git_text = ops['_git_text']
    _parse_checkpoint_time = ops['_parse_checkpoint_time']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths = ops['_validate_attempt_paths']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    datetime = ops['datetime']
    json = ops['json']
    timezone = ops['timezone']
    """Adopt one uniquely proven applied attempt for a newer finding.

    Patch files only narrow the candidate set. ``clawpatch show`` must independently
    return the same attempt before any checkpoint ownership is changed.
    """
    if progress_record.get("phase") != "stopped" or env is None:
        return None
    source_paths = _source_paths(repo)
    if not source_paths:
        return None
    checkpoint_finding = str(progress_record.get("finding_id", ""))
    checkpoint_time = _parse_checkpoint_time(progress_record.get("updated_at"))
    if checkpoint_time is None:
        return None
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    patch_root = repo / ".clawpatch" / "patches"
    if patch_root.is_symlink() or not patch_root.is_dir():
        return None
    try:
        patch_paths = sorted(patch_root.iterdir(), key=lambda path: path.name)
    except OSError:
        return None
    candidates: list[dict[str, Any]] = []
    seen_attempts: set[str] = set()
    for patch_path in patch_paths:
        if patch_path.suffix != ".json":
            continue
        if patch_path.is_symlink() or not patch_path.is_file():
            return None
        try:
            candidate = json.loads(patch_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(candidate, dict):
            return None
        files_changed = candidate.get("filesChanged")
        if (
            not isinstance(files_changed, list)
            or any(not isinstance(path, str) or not path for path in files_changed)
            or sorted(files_changed) != source_paths
        ):
            continue
        try:
            _validate_attempt_paths_syntax(files_changed)
        except SafetyError:
            return None
        finding_ids = candidate.get("findingIds")
        patch_attempt_id = candidate.get("patchAttemptId")
        git_record = candidate.get("git")
        updated_at = _parse_checkpoint_time(candidate.get("updatedAt"))
        if (
            candidate.get("status") != "applied"
            or not isinstance(finding_ids, list)
            or len(finding_ids) != 1
            or not isinstance(finding_ids[0], str)
            or not _FINDING_ID.fullmatch(finding_ids[0])
            or finding_ids[0] == checkpoint_finding
            or not isinstance(patch_attempt_id, str)
            or not patch_attempt_id
            or not isinstance(git_record, dict)
            or updated_at is None
            or updated_at <= checkpoint_time
            or not _attempt_base_preserves_owned_source(
                repo,
                attempt_base=git_record.get("baseSha"),
                current_head=current_head,
                owned_paths=source_paths,
            )
        ):
            continue
        if patch_attempt_id in seen_attempts:
            return None
        try:
            if any(
                (repo / path).is_symlink()
                or not (repo / path).is_file()
                or datetime.fromtimestamp((repo / path).stat().st_mtime, timezone.utc) > updated_at
                for path in source_paths
            ):
                continue
        except OSError:
            continue
        seen_attempts.add(patch_attempt_id)
        candidates.append(candidate)
    if len(candidates) != 1:
        return None
    recorded = candidates[0]
    finding_id = str(recorded["findingIds"][0])
    inspected = _show_finding(
        repo,
        finding_id,
        env=env,
        required_status=None,
        progress=progress,
        current=1,
        total="?",
    )
    if inspected["finding"].get("id") != finding_id or inspected["finding"].get("status") not in {
        "open",
        "uncertain",
        "fixed",
        "false-positive",
    }:
        return None
    matching_attempts = []
    recorded_updated_at = _parse_checkpoint_time(recorded.get("updatedAt"))
    for attempt in inspected["patchAttempts"]:
        if not isinstance(attempt, dict):
            continue
        git_record = attempt.get("git")
        files_changed = attempt.get("filesChanged")
        confirmed_updated_at = _parse_checkpoint_time(attempt.get("updatedAt"))
        if (
            attempt.get("patchAttemptId") == recorded["patchAttemptId"]
            and attempt.get("status") == "applied"
            and attempt.get("findingIds") == [finding_id]
            and isinstance(files_changed, list)
            and all(isinstance(path, str) and path for path in files_changed)
            and sorted(files_changed) == source_paths
            and confirmed_updated_at is not None
            and confirmed_updated_at == recorded_updated_at
            and confirmed_updated_at > checkpoint_time
            and isinstance(git_record, dict)
            and _attempt_base_preserves_owned_source(
                repo,
                attempt_base=git_record.get("baseSha"),
                current_head=current_head,
                owned_paths=source_paths,
            )
        ):
            try:
                edited_after_confirmation = any(
                    (repo / path).is_symlink()
                    or not (repo / path).is_file()
                    or datetime.fromtimestamp((repo / path).stat().st_mtime, timezone.utc)
                    > confirmed_updated_at
                    for path in source_paths
                )
            except OSError:
                continue
            if not edited_after_confirmation:
                matching_attempts.append(attempt)
    if len(matching_attempts) != 1:
        return None
    _validate_attempt_paths(repo, source_paths)
    return {
        "finding_id": finding_id,
        "patch_attempt": matching_attempts[0],
        "patch_attempts": matching_attempts,
        "inspection": inspected,
        "owned_paths": source_paths,
    }


def _impl_checkpoint_later_applied_attempt(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
    *,
    inspected: dict[str, Any],
    env: dict[str, str] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    _checkpoint_cross_finding_applied_attempt = ops['_checkpoint_cross_finding_applied_attempt']
    _checkpoint_same_finding_later_applied_attempt = ops['_checkpoint_same_finding_later_applied_attempt']
    same_finding = _checkpoint_same_finding_later_applied_attempt(
        repo,
        progress_record,
        inspected=inspected,
    )
    if same_finding is not None:
        return same_finding
    return _checkpoint_cross_finding_applied_attempt(
        repo,
        progress_record,
        env=env,
        progress=progress,
    )


def _impl_checkpoint_fixed_without_source(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
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


def _impl_checkpoint_false_positive_without_source(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
    *,
    env: dict[str, str],
    progress: Callable[[dict[str, Any]], None] | None = None,
    inspected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    RepairAction = ops['RepairAction']
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _show_finding = ops['_show_finding']
    _source_paths = ops['_source_paths']
    _verify_iteration_commit = ops['_verify_iteration_commit']
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


def _impl_clear_release_progress(
    ops: Any,
    repo: Path, *, state_root: Path | None = None,
) -> None:
    _release_progress_path = ops['_release_progress_path']
    _release_progress_path(repo, state_root=state_root).unlink(missing_ok=True)


def _impl_recover_external_interrupted_state(
    ops: Any,
    repo: Path,
    *,
    reason: str,
    adopt_dirty: bool = False,
) -> dict[str, Any] | None:
    DirtySourcePolicy = ops['DirtySourcePolicy']
    SafetyError = ops['SafetyError']
    _clear_release_progress = ops['_clear_release_progress']
    _commit_preexisting_source_baseline = ops['_commit_preexisting_source_baseline']
    _git_text = ops['_git_text']
    _load_release_progress = ops['_load_release_progress']
    _release_progress_path = ops['_release_progress_path']
    _source_paths = ops['_source_paths']
    _source_paths_fingerprint = ops['_source_paths_fingerprint']
    atomic_write_json = ops['atomic_write_json']
    external_state_root = ops['external_state_root']
    hashlib = ops['hashlib']
    json = ops['json']
    utc_now = ops['utc_now']
    """Turn supervisor-owned interrupted state into a fresh-review boundary.

    The external command owns this checkpoint, not the project source.  Visible
    source becomes an ordinary input-baseline commit; a source-clean checkpoint
    is retired directly.  In both cases the next sweep rebuilds and reviews the
    queue instead of requiring operator recovery.
    """
    root = repo.resolve()
    state_root = external_state_root(root)
    progress_path = _release_progress_path(root, state_root=state_root)
    quarantined_checkpoint = ""
    try:
        checkpoint = _load_release_progress(root, state_root=state_root)
    except SafetyError:
        if progress_path.is_symlink() or not progress_path.is_file():
            raise
        raw_progress = progress_path.read_bytes()
        digest = hashlib.sha256(raw_progress).hexdigest()
        quarantine_path = state_root / "recoveries" / f"invalid-progress-{digest}.json"
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        if quarantine_path.exists():
            if quarantine_path.is_symlink() or quarantine_path.read_bytes() != raw_progress:
                raise SafetyError(
                    "Malformed Clawpatch checkpoint quarantine does not match its digest."
                )
            progress_path.unlink()
        else:
            progress_path.replace(quarantine_path)
        quarantined_checkpoint = str(quarantine_path)
        checkpoint = None
    queue_path = root / ".clawpatch" / "project.json"
    queue_exists = queue_path.is_file() and not queue_path.is_symlink()
    if checkpoint is None and not quarantined_checkpoint and not queue_exists:
        return None
    source_paths = _source_paths(root)
    DirtySourcePolicy(adopt_dirty=adopt_dirty).require_authorized(
        root,
        source_paths,
        context="Interrupted supervisor recovery",
    )
    current_head = _git_text(root, ["git", "rev-parse", "HEAD"])
    failure = reason[-4000:]
    boundary = {
        "repo": str(root),
        "head": current_head,
        "paths": source_paths,
        "source_fingerprint": (
            _source_paths_fingerprint(root, source_paths) if source_paths else ""
        ),
        "failure": failure,
    }
    boundary_key = hashlib.sha256(
        json.dumps(boundary, sort_keys=True).encode("utf-8")
    ).hexdigest()
    receipt_path = state_root / "recoveries" / f"unattended-{boundary_key}.json"
    if receipt_path.exists():
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise SafetyError("Unattended Clawpatch recovery receipt is unsafe.")
        return None
    baseline = (
        _commit_preexisting_source_baseline(root, source_paths, state_root=state_root)
        if source_paths
        else {}
    )
    receipt = {
        "version": 1,
        "reason": "unattended-checkpoint-recovery",
        "repo": str(root),
        "finding_id": str(checkpoint["finding_id"]) if checkpoint else "unknown",
        "checkpoint": checkpoint or {},
        "quarantined_checkpoint": quarantined_checkpoint,
        "paths": source_paths,
        "baseline_commit": str(baseline.get("baseline_commit", "")),
        "failure": failure,
        "created_at": utc_now(),
    }
    atomic_write_json(receipt_path, receipt)
    _clear_release_progress(root, state_root=state_root)
    return {
        "finding_id": str(checkpoint["finding_id"]) if checkpoint else "unknown",
        "paths": source_paths,
        "baseline_commit": str(baseline.get("baseline_commit", "")),
        "receipt": str(receipt_path),
        "quarantined_checkpoint": quarantined_checkpoint,
    }


def _impl_parse_checkpoint_time(
    ops: Any,
    value: Any,
) -> datetime | None:
    datetime = ops['datetime']
    timezone = ops['timezone']
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _impl_empty_clawpatch_history(
    ops: Any,
    repo: Path,
) -> bool:
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


def _impl_rebuilt_generation_owns_checkpoint_source(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
) -> bool:
    _empty_clawpatch_history = ops['_empty_clawpatch_history']
    _git_text = ops['_git_text']
    _owned_source_fingerprint = ops['_owned_source_fingerprint']
    _parse_checkpoint_time = ops['_parse_checkpoint_time']
    _source_paths = ops['_source_paths']
    datetime = ops['datetime']
    json = ops['json']
    timezone = ops['timezone']
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


def _impl_rebuilt_generation_supersedes_empty_checkpoint(
    ops: Any,
    repo: Path,
    progress_record: dict[str, Any],
) -> bool:
    _git_text = ops['_git_text']
    _parse_checkpoint_time = ops['_parse_checkpoint_time']
    _run = ops['_run']
    _source_paths = ops['_source_paths']
    json = ops['json']
    re = ops['re']
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


def _impl_exclude_gitlinks_from_clawpatch_config(
    ops: Any,
    repo: Path,
) -> list[str]:
    SafetyError = ops['SafetyError']
    _gitlink_paths = ops['_gitlink_paths']
    atomic_write_json = ops['atomic_write_json']
    json = ops['json']
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


def _impl_fresh_checkpoint_owned_paths(
    ops: Any,
    repo: Path,
    source_changes: list[str],
    *,
    state_root: Path | None = None,
) -> list[str]:
    SafetyError = ops['SafetyError']
    _checkpoint_can_follow_supervisor_upgrade = ops['_checkpoint_can_follow_supervisor_upgrade']
    _checkpoint_proves_exact_source = ops['_checkpoint_proves_exact_source']
    _git_text = ops['_git_text']
    _load_release_progress = ops['_load_release_progress']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
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


def _impl_checkpoint_proves_exact_source(
    ops: Any,
    repo: Path,
    checkpoint: dict[str, Any],
    paths: list[str],
) -> bool:
    _owned_source_fingerprint = ops['_owned_source_fingerprint']
    _source_paths = ops['_source_paths']
    _temporary_commit_matches_owned_source = ops['_temporary_commit_matches_owned_source']
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


def _impl_commit_ambiguous_checkpoint_source_baseline(
    ops: Any,
    repo: Path,
    checkpoint: dict[str, Any],
    paths: list[str],
    *,
    state_root: Path,
) -> dict[str, Any] | None:
    Path = ops['Path']
    _clear_release_progress = ops['_clear_release_progress']
    _commit_preexisting_source_baseline = ops['_commit_preexisting_source_baseline']
    _source_paths = ops['_source_paths']
    atomic_write_json = ops['atomic_write_json']
    json = ops['json']
    """Make ambiguous stopped source the new input baseline and retire its wrapper."""
    exact_paths = sorted(set(paths))
    owned_paths = sorted(str(path) for path in checkpoint.get("owned_paths", []))
    if (
        checkpoint.get("phase") != "stopped"
        or not exact_paths
        or exact_paths != _source_paths(repo)
    ):
        return None
    baseline = _commit_preexisting_source_baseline(
        repo,
        exact_paths,
        state_root=state_root,
    )
    receipt_path = Path(str(baseline["receipt"]))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt.update(
        {
            "reason": "ambiguous-checkpoint-source-baseline",
            "finding_id": str(checkpoint["finding_id"]),
            "checkpoint_head": str(checkpoint.get("head_before", "")),
            "checkpoint_owned_paths": owned_paths,
            "recorded_source_fingerprint": str(
                checkpoint.get("owned_source_fingerprint", "")
            ),
            "checkpoint": checkpoint,
        }
    )
    atomic_write_json(receipt_path, receipt)
    _clear_release_progress(repo, state_root=state_root)
    return {
        "finding_id": str(checkpoint["finding_id"]),
        **baseline,
    }


def _impl_commit_preexisting_source_baseline(
    ops: Any,
    repo: Path,
    paths: list[str],
    *,
    state_root: Path,
) -> dict[str, Any]:
    Path = ops['Path']
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _paths_between = ops['_paths_between']
    _run = ops['_run']
    _source_paths = ops['_source_paths']
    _source_paths_fingerprint = ops['_source_paths_fingerprint']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
    atomic_write_json = ops['atomic_write_json']
    current_temporary_root = ops['current_temporary_root']
    os = ops['os']
    tempfile = ops['tempfile']
    utc_now = ops['utc_now']
    """Commit the complete current source tree as the external run baseline."""
    exact_paths = sorted(set(paths))
    if not exact_paths or exact_paths != _source_paths(repo):
        raise SafetyError(
            "Automatic Clawpatch baseline creation requires the complete current source set."
        )
    _validate_attempt_paths_syntax(exact_paths)
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    source_fingerprint = _source_paths_fingerprint(repo, exact_paths)

    temporary_root = current_temporary_root()
    with tempfile.TemporaryDirectory(
        prefix="clawpatch-supervise-baseline-index-",
        dir=str(temporary_root) if temporary_root is not None else None,
    ) as temp:
        index_path = Path(temp) / "index"
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(index_path)
        env.update(
            {
                "GIT_AUTHOR_NAME": "ClawPatch Supervise Baseline",
                "GIT_AUTHOR_EMAIL": "clawpatch-supervise-baseline@localhost",
                "GIT_COMMITTER_NAME": "ClawPatch Supervise Baseline",
                "GIT_COMMITTER_EMAIL": "clawpatch-supervise-baseline@localhost",
            }
        )
        _must_run(["git", "read-tree", current_head], cwd=repo, timeout=120, env=env)
        _must_run(["git", "add", "-A", "--", *exact_paths], cwd=repo, timeout=120, env=env)
        baseline_tree = _must_run(
            ["git", "write-tree"], cwd=repo, timeout=120, env=env
        ).strip()
        baseline_commit = _must_run(
            [
                "git",
                "commit-tree",
                baseline_tree,
                "-p",
                current_head,
                "-m",
                "clawpatch baseline: pre-existing source",
            ],
            cwd=repo,
            timeout=120,
            env=env,
        ).strip()
    if _paths_between(repo, current_head, baseline_commit) != exact_paths:
        raise SafetyError(
            "Automatic Clawpatch baseline could not commit exactly the current source paths."
        )

    baseline_ref = f"refs/clawpatch-supervise/baselines/{baseline_commit}"
    existing_ref = _run(
        ["git", "rev-parse", "--verify", baseline_ref],
        cwd=repo,
        timeout=60,
    )
    if existing_ref.returncode:
        _must_run(
            ["git", "update-ref", baseline_ref, baseline_commit],
            cwd=repo,
            timeout=60,
        )
    elif existing_ref.stdout.strip() != baseline_commit:
        raise SafetyError("Clawpatch baseline ref unexpectedly points to different source.")

    receipt_path = state_root / "baselines" / f"{baseline_commit}.json"
    receipt = {
        "version": 1,
        "reason": "preexisting-source-baseline",
        "repo": str(repo.resolve()),
        "parent_head": current_head,
        "paths": exact_paths,
        "source_fingerprint": source_fingerprint,
        "baseline_tree": baseline_tree,
        "baseline_commit": baseline_commit,
        "baseline_ref": baseline_ref,
        "created_at": utc_now(),
    }
    atomic_write_json(receipt_path, receipt)

    _must_run(
        ["git", "update-ref", "HEAD", baseline_commit, current_head],
        cwd=repo,
        timeout=60,
    )
    _must_run(["git", "reset", "--mixed", "--quiet", "HEAD"], cwd=repo, timeout=120)
    if _git_text(repo, ["git", "rev-parse", "HEAD"]) != baseline_commit:
        raise SafetyError("Automatic Clawpatch baseline did not become the current Git HEAD.")
    remaining_source = _source_paths(repo)
    if remaining_source or _git_text(repo, ["git", "rev-parse", "HEAD^{tree}"]) != baseline_tree:
        raise SafetyError(
            "Automatic Clawpatch baseline did not leave the complete input source as a clean "
            "current HEAD: " + ", ".join(remaining_source)
        )
    return {
        "paths": exact_paths,
        "parent_head": current_head,
        "baseline_commit": baseline_commit,
        "baseline_ref": baseline_ref,
        "receipt": str(receipt_path),
    }


def _impl_recover_checkpoint_temporary_commit(
    ops: Any,
    repo: Path,
    *,
    state_root: Path | None = None,
) -> None:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _load_release_progress = ops['_load_release_progress']
    _must_run = ops['_must_run']
    _run = ops['_run']
    _source_paths = ops['_source_paths']
    _verify_iteration_commit = ops['_verify_iteration_commit']
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
            and temporary_is_ancestor.returncode in {0, 1}
        ):
            # The verified iteration may either be a dangling sibling or already
            # committed in the current clean history. Both are safe to retire.
            return
        raise SafetyError(
            "Interrupted Clawpatch temporary commit no longer matches the current Git HEAD."
        )
    recovered_paths = _source_paths(repo)
    if recovered_paths != owned_paths:
        raise SafetyError(
            "Recovered Clawpatch temporary commit does not expose exactly its checkpoint paths."
        )


def _impl_discard_checkpoint_owned_source(
    ops: Any,
    repo: Path, paths: list[str],
) -> None:
    SafetyError = ops['SafetyError']
    _must_run = ops['_must_run']
    _source_paths = ops['_source_paths']
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
