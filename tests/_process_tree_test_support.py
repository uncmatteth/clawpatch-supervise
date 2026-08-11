from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_group_exists(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_descendant_exit(pid: int, group_id: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _pid_exists(pid) or _process_group_exists(group_id):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def assert_blocked_descendant_exited(
    *,
    ready: Path,
    release: Path,
    sentinel: Path,
    timeout: float = 5,
) -> None:
    descendant_pid, process_group_id = map(
        int,
        ready.read_text(encoding="utf-8").split(),
    )
    if process_group_id == os.getpgrp():
        raise AssertionError("descendant unexpectedly shares the test process group")

    release.touch()
    try:
        if not _wait_for_descendant_exit(
            descendant_pid,
            process_group_id,
            timeout=timeout,
        ):
            raise AssertionError(
                "timed-out descendant remained alive: "
                f"pid={descendant_pid}, process_group={process_group_id}"
            )
        if sentinel.exists():
            raise AssertionError("timed-out descendant escaped process-group termination")
    finally:
        if _pid_exists(descendant_pid) or _process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _wait_for_descendant_exit(
                descendant_pid,
                process_group_id,
                timeout=timeout,
            )
