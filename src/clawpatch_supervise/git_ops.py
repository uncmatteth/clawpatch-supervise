from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import SafetyError


@dataclass(frozen=True)
class DirtySourcePolicy:
    """The complete interface for deciding whether existing source may be adopted."""

    adopt_dirty: bool = False

    def require_authorized(self, repo: Path, paths: list[str], *, context: str) -> None:
        if paths and not self.adopt_dirty:
            joined = ", ".join(paths)
            raise SafetyError(
                f"{context} found pre-existing dirty source in {repo}: {joined}. "
                "Commit it yourself or run again with --adopt-dirty to make one explicit "
                "input-baseline commit."
            )


# Release-engine component implementations. The compatibility facade remains in clawpatch_release.

def _impl_git_root(
    ops: Any,
    repo: Path,
) -> Path:
    Path = ops['Path']
    SafetyError = ops['SafetyError']
    _run = ops['_run']
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=repo, timeout=30)
    if result.returncode or not result.stdout.strip():
        raise SafetyError("Clawpatch release sweep requires an existing Git repository.")
    return Path(result.stdout.strip()).resolve()


def _impl_git_text(
    ops: Any,
    repo: Path, argv: list[str],
) -> str:
    _must_run = ops['_must_run']
    return _must_run(argv, cwd=repo, timeout=600).strip()


def _impl_require_branch(
    ops: Any,
    repo: Path, expected: str, *, phase: str,
) -> None:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    current = _git_text(repo, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if current != expected:
        raise SafetyError(
            f"Git branch changed during Clawpatch {phase}; expected {expected!r}, found {current!r}."
        )


def _impl_clawpatch_state_fingerprint(
    ops: Any,
    root: Path,
) -> str:
    SafetyError = ops['SafetyError']
    hashlib = ops['hashlib']
    os = ops['os']
    stat = ops['stat']
    digest = hashlib.sha256()
    pending = [(root, ".")]
    while pending:
        path, relative = pending.pop()
        metadata = path.lstat()
        encoded_relative = os.fsencode(relative)
        digest.update(len(encoded_relative).to_bytes(8, "big"))
        digest.update(encoded_relative)
        if stat.S_ISLNK(metadata.st_mode):
            target = os.fsencode(os.readlink(path))
            digest.update(b"L")
            digest.update(len(target).to_bytes(8, "big"))
            digest.update(target)
        elif stat.S_ISDIR(metadata.st_mode):
            digest.update(b"D")
            children = sorted(path.iterdir(), key=lambda item: os.fsencode(item.name))
            pending.extend(
                (child, f"{relative}/{child.name}") for child in reversed(children)
            )
        elif stat.S_ISREG(metadata.st_mode):
            file_digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    file_digest.update(block)
            digest.update(b"F")
            digest.update(file_digest.digest())
        else:
            raise SafetyError("ClawPatch state contains an unsupported filesystem entry.")
    return digest.hexdigest()


def _impl_hard_reset_preserving_clawpatch_state(
    ops: Any,
    repo: Path, target: str,
) -> None:
    Path = ops['Path']
    SafetyError = ops['SafetyError']
    _clawpatch_state_fingerprint = ops['_clawpatch_state_fingerprint']
    _must_run = ops['_must_run']
    shutil = ops['shutil']
    tempfile = ops['tempfile']
    state_root = repo / ".clawpatch"
    if state_root.is_symlink() or (state_root.exists() and not state_root.is_dir()):
        raise SafetyError(
            "Divergent-history recovery requires .clawpatch to be a safe repository directory."
        )
    if not state_root.is_dir():
        _must_run(["git", "reset", "--hard", target], cwd=repo, timeout=120)
        return

    recovery_root = Path(tempfile.mkdtemp(prefix="clawpatch-supervise-state-recovery-"))
    snapshot = recovery_root / "clawpatch"
    try:
        shutil.copytree(state_root, snapshot, symlinks=True)
        snapshot_fingerprint = _clawpatch_state_fingerprint(snapshot)
    except BaseException:
        shutil.rmtree(recovery_root, ignore_errors=True)
        raise

    restored = False
    try:
        try:
            _must_run(["git", "reset", "--hard", target], cwd=repo, timeout=120)
        finally:
            try:
                with tempfile.TemporaryDirectory(
                    prefix=".clawpatch-restore-", dir=repo
                ) as staging_root:
                    staged_state = Path(staging_root) / "clawpatch"
                    shutil.copytree(snapshot, staged_state, symlinks=True)
                    if _clawpatch_state_fingerprint(staged_state) != snapshot_fingerprint:
                        raise SafetyError(
                            "The staged ClawPatch state does not match its recovery snapshot."
                        )
                    if state_root.is_symlink() or (
                        state_root.exists() and not state_root.is_dir()
                    ):
                        state_root.unlink()
                    elif state_root.exists():
                        shutil.rmtree(state_root)
                    staged_state.rename(state_root)
                    if _clawpatch_state_fingerprint(state_root) != snapshot_fingerprint:
                        raise SafetyError(
                            "The restored ClawPatch state does not match its recovery snapshot."
                        )
            except (OSError, SafetyError) as exc:
                raise SafetyError(
                    "ClawPatch state restoration failed; recovery snapshot retained at "
                    f"{snapshot}: {exc}"
                ) from exc
            restored = True
    finally:
        if restored:
            shutil.rmtree(recovery_root)


def _impl_require_synchronized_remote_branch(
    ops: Any,
    repo: Path,
    branch: str,
    *,
    progress: Callable[[dict[str, Any]], None] | None = None,
    preserve_local_on_conflict: bool = False,
) -> str:
    RepositoryBusyError = ops['RepositoryBusyError']
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _hard_reset_preserving_clawpatch_state = ops['_hard_reset_preserving_clawpatch_state']
    _must_run = ops['_must_run']
    _require_branch = ops['_require_branch']
    _run = ops['_run']
    _source_paths = ops['_source_paths']
    tempfile = ops['tempfile']
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
            raise RepositoryBusyError(
                f"Local HEAD {local!r} and origin/{branch} at {remote!r} cannot be "
                "reconciled while preserved source changes are present. Waiting without "
                "discarding either history or the worktree."
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
        remote_is_ancestor = _run(
            ["git", "merge-base", "--is-ancestor", remote, local],
            cwd=repo,
            timeout=60,
        )
        if remote_is_ancestor.returncode == 0:
            return local
        ancestor = _run(
            ["git", "merge-base", "--is-ancestor", local, remote],
            cwd=repo,
            timeout=60,
        )
        if ancestor.returncode:
            if progress is not None:
                progress(
                    {
                        "phase": "git-sync",
                        "current": "?",
                        "total": "?",
                        "command": f"git merge --no-ff --no-edit {remote}",
                        "detail": f"merge clean divergent {branch} and origin/{branch}",
                        "attempt": 1,
                        "max_attempts": 1,
                    }
                )
            _require_branch(repo, branch, phase="remote synchronization")
            local_before_merge = local
            with tempfile.TemporaryDirectory(prefix="clawpatch-supervise-empty-hooks-") as hooks:
                merged = _run(
                    [
                        "git",
                        "-c",
                        "commit.gpgSign=false",
                        "-c",
                        f"core.hooksPath={hooks}",
                        "merge",
                        "--no-ff",
                        "--no-edit",
                        remote,
                    ],
                    cwd=repo,
                    timeout=300,
                )
            if merged.returncode:
                merge_head = _run(
                    ["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"],
                    cwd=repo,
                    timeout=60,
                )
                if merge_head.returncode == 0:
                    _must_run(["git", "merge", "--abort"], cwd=repo, timeout=120)
                restored = _git_text(repo, ["git", "rev-parse", "HEAD"])
                if restored != local or _source_paths(repo):
                    raise SafetyError(
                        "Automatic divergent-history merge failed and exact pre-merge state "
                        "could not be restored."
                    )
                if preserve_local_on_conflict:
                    raise SafetyError(
                        "The current Clawpatch input baseline conflicts with "
                        f"origin/{branch}. The baseline remains current at {local}; resolve the "
                        "real Git conflict before supervision continues."
                    )
                recovery_ref = (
                    "refs/clawpatch-supervise/recovery/diverged-history/" + local
                )
                existing_recovery = _run(
                    ["git", "rev-parse", "--verify", recovery_ref],
                    cwd=repo,
                    timeout=60,
                )
                if existing_recovery.returncode:
                    _must_run(
                        ["git", "update-ref", recovery_ref, local],
                        cwd=repo,
                        timeout=60,
                    )
                elif existing_recovery.stdout.strip() != local:
                    raise SafetyError(
                        "Divergent-history recovery ref points to unexpected local history."
                    )
                if _git_text(repo, ["git", "rev-parse", recovery_ref]) != local:
                    raise SafetyError("Divergent local history could not be preserved.")
                final_remote_line = _git_text(
                    repo, ["git", "ls-remote", "origin", f"refs/heads/{branch}"]
                )
                final_remote = final_remote_line.split()[0] if final_remote_line else ""
                if final_remote != remote:
                    raise RepositoryBusyError(
                        f"Origin/{branch} changed during divergent-history recovery; "
                        "retrying from the preserved local history."
                    )
                if progress is not None:
                    progress(
                        {
                            "phase": "git-sync",
                            "current": "?",
                            "total": "?",
                            "command": f"preserve {local}; align {branch} to {remote}",
                            "detail": (
                                "preserve conflicting local history in a recovery ref; "
                                f"continue from origin/{branch}"
                            ),
                            "attempt": 1,
                            "max_attempts": 1,
                            "preserved_ref": recovery_ref,
                        }
                    )
                _hard_reset_preserving_clawpatch_state(repo, remote)
                _require_branch(repo, branch, phase="divergent-history recovery")
                aligned = _git_text(repo, ["git", "rev-parse", "HEAD"])
                if aligned != remote or _source_paths(repo):
                    raise SafetyError(
                        "Divergent-history recovery did not leave the exact remote source tree."
                    )
                return aligned
            _require_branch(repo, branch, phase="remote synchronization")
            local = _git_text(repo, ["git", "rev-parse", "HEAD"])
            if _source_paths(repo):
                raise SafetyError("Automatic divergent-history merge left source changes.")
            for ancestor_sha in (local_before_merge, remote):
                included = _run(
                    ["git", "merge-base", "--is-ancestor", ancestor_sha, local],
                    cwd=repo,
                    timeout=60,
                )
                if included.returncode:
                    raise SafetyError(
                        "Automatic divergent-history merge did not preserve both histories."
                    )
            final_remote_line = _git_text(
                repo, ["git", "ls-remote", "origin", f"refs/heads/{branch}"]
            )
            final_remote = final_remote_line.split()[0] if final_remote_line else ""
            if final_remote != remote:
                raise RepositoryBusyError(
                    f"Origin/{branch} changed during divergent-history reconciliation; "
                    "waiting to retry from the preserved merged history."
                )
            return local
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


def _impl_status_entries(
    ops: Any,
    repo: Path,
) -> list[tuple[str, str]]:
    SafetyError = ops['SafetyError']
    _must_run = ops['_must_run']
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


def _impl_status_paths(
    ops: Any,
    repo: Path,
) -> list[str]:
    _status_entries = ops['_status_entries']
    return sorted({path for _status, path in _status_entries(repo)})


def _impl_is_untracked_dependency_path(
    ops: Any,
    status: str, path: str,
) -> bool:
    PurePosixPath = ops['PurePosixPath']
    return status == "??" and "node_modules" in PurePosixPath(path).parts


def _impl_source_paths(
    ops: Any,
    repo: Path,
) -> list[str]:
    _is_untracked_dependency_path = ops['_is_untracked_dependency_path']
    _status_entries = ops['_status_entries']
    return sorted(
        {
            path
            for status, path in _status_entries(repo)
            if path != ".clawpatch"
            and not path.startswith(".clawpatch/")
            and not _is_untracked_dependency_path(status, path)
        }
    )


def _impl_normalized_stopped_owned_paths(
    ops: Any,
    repo: Path,
    checkpoint: dict[str, Any],
    recorded_paths: list[str],
) -> list[str]:
    _is_untracked_dependency_path = ops['_is_untracked_dependency_path']
    _source_paths = ops['_source_paths']
    _source_paths_fingerprint = ops['_source_paths_fingerprint']
    _status_entries = ops['_status_entries']
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


def _impl_gitlink_paths(
    ops: Any,
    repo: Path,
) -> list[str]:
    SafetyError = ops['SafetyError']
    _must_run = ops['_must_run']
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


def _impl_source_state_fingerprint_for_paths(
    ops: Any,
    repo: Path, paths: list[str],
) -> dict[str, Any]:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _gitlink_paths = ops['_gitlink_paths']
    _must_run = ops['_must_run']
    _source_state_fingerprint = ops['_source_state_fingerprint']
    _untracked_path_fingerprint = ops['_untracked_path_fingerprint']
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


def _impl_untracked_path_fingerprint(
    ops: Any,
    repo: Path, path: str,
) -> str:
    _git_text = ops['_git_text']
    hashlib = ops['hashlib']
    os = ops['os']
    candidate = repo / path
    if candidate.is_symlink():
        target = os.readlink(candidate)
        digest = hashlib.sha256(os.fsencode(target)).hexdigest()
        return f"symlink:{digest}"
    return _git_text(repo, ["git", "hash-object", "--no-filters", "--", path])


def _impl_source_state_fingerprint(
    ops: Any,
    repo: Path,
) -> dict[str, Any]:
    _source_paths = ops['_source_paths']
    _source_state_fingerprint_for_paths = ops['_source_state_fingerprint_for_paths']
    return _source_state_fingerprint_for_paths(repo, _source_paths(repo))


def _impl_source_paths_fingerprint(
    ops: Any,
    repo: Path, paths: list[str],
) -> str:
    _source_state_fingerprint_for_paths = ops['_source_state_fingerprint_for_paths']
    hashlib = ops['hashlib']
    json = ops['json']
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


def _impl_legacy_owned_source_fingerprint(
    ops: Any,
    repo: Path, paths: list[str],
) -> str:
    _source_paths = ops['_source_paths']
    _source_state_fingerprint = ops['_source_state_fingerprint']
    hashlib = ops['hashlib']
    json = ops['json']
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


def _impl_owned_source_fingerprint(
    ops: Any,
    repo: Path, paths: list[str],
) -> str:
    _source_paths = ops['_source_paths']
    _source_paths_fingerprint = ops['_source_paths_fingerprint']
    exact_paths = sorted(set(paths))
    if not exact_paths or _source_paths(repo) != exact_paths:
        return ""
    return _source_paths_fingerprint(repo, exact_paths)


def _impl_committed_clawpatch_config(
    ops: Any,
    repo: Path,
) -> str | None:
    _run = ops['_run']
    current = repo / ".clawpatch" / "config.json"
    if current.is_file():
        return current.read_text(encoding="utf-8")
    result = _run(
        ["git", "show", "HEAD:.clawpatch/config.json"],
        cwd=repo,
        timeout=60,
    )
    return result.stdout if result.returncode == 0 and result.stdout.strip() else None


def _impl_current_input_baseline_commit(
    ops: Any,
    repo: Path,
) -> str:
    _git_text = ops['_git_text']
    _run = ops['_run']
    """Return current HEAD only when a durable local baseline ref identifies it."""
    current_head = _git_text(repo, ["git", "rev-parse", "HEAD"])
    baseline_ref = f"refs/clawpatch-supervise/baselines/{current_head}"
    resolved = _run(
        ["git", "rev-parse", "--verify", baseline_ref],
        cwd=repo,
        timeout=60,
    )
    return current_head if resolved.returncode == 0 and resolved.stdout.strip() == current_head else ""


def _impl_temporary_commit_matches_owned_source(
    ops: Any,
    repo: Path,
    *,
    original_head: str,
    temporary_commit: str,
    paths: list[str],
) -> bool:
    Path = ops['Path']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    current_temporary_root = ops['current_temporary_root']
    os = ops['os']
    tempfile = ops['tempfile']
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


def _impl_commit_attempt(
    ops: Any,
    repo: Path,
    finding_id: str,
    files: list[str],
    *,
    branch: str,
    outcome: str = "fixed",
) -> str:
    SafetyError = ops['SafetyError']
    _commit_without_local_hooks = ops['_commit_without_local_hooks']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _require_branch = ops['_require_branch']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths = ops['_validate_attempt_paths']
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


def _impl_commit_without_local_hooks(
    ops: Any,
    repo: Path, *args: str,
) -> None:
    _must_run = ops['_must_run']
    current_temporary_root = ops['current_temporary_root']
    tempfile = ops['tempfile']
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


def _impl_paths_between(
    ops: Any,
    repo: Path, start: str, end: str = "HEAD",
) -> list[str]:
    _must_run = ops['_must_run']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
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


def _impl_verify_iteration_commit(
    ops: Any,
    repo: Path,
    *,
    finding_id: str,
    original_head: str,
    temporary_commit: str,
    require_current: bool = True,
) -> list[str]:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _paths_between = ops['_paths_between']
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


def _impl_stage_current_source(
    ops: Any,
    repo: Path,
) -> tuple[list[str], str]:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _source_paths = ops['_source_paths']
    _validate_attempt_paths_syntax = ops['_validate_attempt_paths_syntax']
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


def _impl_finalize_finding_commit(
    ops: Any,
    repo: Path,
    *,
    finding_id: str,
    branch: str,
    original_head: str,
    temporary_commit: str,
    seen_states: set[str],
) -> str:
    SafetyError = ops['SafetyError']
    _UnresolvedFinding = ops['_UnresolvedFinding']
    _commit_attempt = ops['_commit_attempt']
    _commit_without_local_hooks = ops['_commit_without_local_hooks']
    _git_text = ops['_git_text']
    _paths_between = ops['_paths_between']
    _require_branch = ops['_require_branch']
    _source_paths = ops['_source_paths']
    _stage_current_source = ops['_stage_current_source']
    _verify_iteration_commit = ops['_verify_iteration_commit']
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


def _impl_push_and_verify(
    ops: Any,
    repo: Path, branch: str, *, first: bool,
) -> None:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _require_branch = ops['_require_branch']
    _require_branch(repo, branch, phase="push")
    argv = ["git", "push", "-u", "origin", branch] if first else ["git", "push", "origin", branch]
    _must_run(argv, cwd=repo, timeout=600)
    local = _git_text(repo, ["git", "rev-parse", "HEAD"])
    remote_line = _git_text(repo, ["git", "ls-remote", "origin", f"refs/heads/{branch}"])
    remote = remote_line.split()[0] if remote_line else ""
    if remote != local:
        raise SafetyError(f"Live remote branch SHA {remote!r} does not equal local HEAD {local!r}.")


def _impl_publish_final_state(
    ops: Any,
    repo: Path, *, branch: str,
) -> str:
    SafetyError = ops['SafetyError']
    _git_text = ops['_git_text']
    _must_run = ops['_must_run']
    _require_branch = ops['_require_branch']
    _status_paths = ops['_status_paths']
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
