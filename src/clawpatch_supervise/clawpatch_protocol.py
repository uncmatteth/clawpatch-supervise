from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ClawpatchFailureKind(str, Enum):
    PROVIDER_FAILED = "provider-failed"
    INVALID_USAGE = "invalid-usage"
    DIRTY_WORKTREE = "dirty-worktree"
    PROVIDER_REFUSED = "provider-refused"
    PROVIDER_QUOTA = "provider-quota"
    VALIDATION_FAILED = "validation-failed"
    TIMEOUT = "timeout"
    COMMAND_FAILED = "command-failed"


class RepairAction(str, Enum):
    FINALIZE = "finalize"
    PRESERVE_AND_CONTINUE = "preserve-and-continue"
    COMMIT_AND_ADVANCE = "commit-and-advance"
    DISCARD_AND_CONTINUE = "discard-and-continue"
    STOP_TRANSIENT = "stop-transient"
    STOP_TERMINAL = "stop-terminal"


@dataclass(frozen=True)
class ClawpatchFailure:
    phase: str
    exit_code: int
    kind: ClawpatchFailureKind
    transient: bool
    progress_capable: bool


@dataclass(frozen=True)
class RepairDecision:
    action: RepairAction
    reason: str


_EXIT_FAILURES = {
    1: ClawpatchFailureKind.PROVIDER_FAILED,
    2: ClawpatchFailureKind.INVALID_USAGE,
    3: ClawpatchFailureKind.DIRTY_WORKTREE,
    4: ClawpatchFailureKind.PROVIDER_REFUSED,
    5: ClawpatchFailureKind.PROVIDER_QUOTA,
    6: ClawpatchFailureKind.VALIDATION_FAILED,
    124: ClawpatchFailureKind.TIMEOUT,
}
_TRANSIENT_FAILURES = frozenset(
    {
        ClawpatchFailureKind.PROVIDER_FAILED,
        ClawpatchFailureKind.PROVIDER_REFUSED,
        ClawpatchFailureKind.PROVIDER_QUOTA,
        ClawpatchFailureKind.TIMEOUT,
    }
)
_PROGRESS_CAPABLE_FAILURES = frozenset(
    {
        ClawpatchFailureKind.PROVIDER_FAILED,
        ClawpatchFailureKind.PROVIDER_REFUSED,
        ClawpatchFailureKind.PROVIDER_QUOTA,
        ClawpatchFailureKind.VALIDATION_FAILED,
        ClawpatchFailureKind.TIMEOUT,
        ClawpatchFailureKind.COMMAND_FAILED,
    }
)


def classify_clawpatch_failure(phase: str, exit_code: int) -> ClawpatchFailure:
    if not phase or isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code == 0:
        raise ValueError("A failed ClawPatch command requires a phase and a nonzero exit code.")
    kind = _EXIT_FAILURES.get(exit_code, ClawpatchFailureKind.COMMAND_FAILED)
    return ClawpatchFailure(
        phase=phase,
        exit_code=exit_code,
        kind=kind,
        transient=kind in _TRANSIENT_FAILURES,
        progress_capable=kind in _PROGRESS_CAPABLE_FAILURES,
    )


def failure_from_legacy_outcome(outcome: str | None) -> ClawpatchFailure | None:
    mapping = {
        "provider-failed": ("fix", 1),
        "provider-quota": ("fix", 5),
        "fix-validation-failed": ("fix", 6),
        "timeout": ("fix", 124),
        "revalidation-provider-failed": ("revalidation", 4),
        "revalidation-command-failed-with-source-progress": ("revalidation", 23),
        "revalidation-mutated-source": ("revalidation", 23),
    }
    event = mapping.get(outcome)
    return classify_clawpatch_failure(*event) if event is not None else None


def decide_repair_transition(
    *,
    failure: ClawpatchFailure | None = None,
    revalidation_outcome: str | None = None,
    has_source_progress: bool = False,
    advance_uncertain: bool = False,
) -> RepairDecision:
    if (failure is None) == (revalidation_outcome is None):
        raise ValueError("A repair transition requires exactly one ClawPatch event.")
    if failure is not None:
        if has_source_progress and failure.progress_capable:
            return RepairDecision(
                RepairAction.PRESERVE_AND_CONTINUE,
                f"{failure.kind.value}-with-source-progress",
            )
        if failure.transient:
            return RepairDecision(RepairAction.STOP_TRANSIENT, failure.kind.value)
        return RepairDecision(RepairAction.STOP_TERMINAL, failure.kind.value)
    if revalidation_outcome == "fixed":
        return RepairDecision(RepairAction.FINALIZE, "fixed")
    if revalidation_outcome == "open":
        return RepairDecision(RepairAction.PRESERVE_AND_CONTINUE, "open")
    if revalidation_outcome == "false-positive":
        return RepairDecision(RepairAction.DISCARD_AND_CONTINUE, "false-positive")
    if revalidation_outcome == "uncertain" and advance_uncertain and has_source_progress:
        return RepairDecision(RepairAction.COMMIT_AND_ADVANCE, "uncertain-recorded")
    if revalidation_outcome == "uncertain" and has_source_progress:
        return RepairDecision(RepairAction.PRESERVE_AND_CONTINUE, "uncertain-with-source-progress")
    if revalidation_outcome == "uncertain":
        return RepairDecision(RepairAction.STOP_TERMINAL, "uncertain")
    raise ValueError(f"Unsupported ClawPatch revalidation outcome: {revalidation_outcome!r}")
