from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from clawpatch_supervise.clawpatch_external import main
from clawpatch_supervise.cleanup import _pid_is_running


class CleanupCommandTests(unittest.TestCase):
    @staticmethod
    def _mark(candidate: Path, *, pid: int, created_unix: float) -> None:
        candidate.mkdir(parents=True)
        (candidate / ".clawpatch-supervise-owned.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "owner": "clawpatch-supervise",
                    "kind": "run-temp",
                    "directory": candidate.name,
                    "pid": pid,
                    "created_unix": created_unix,
                }
            ),
            encoding="utf-8",
        )

    def test_cleanup_dry_run_reports_owned_stale_directory_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-deadbeef"
            candidate.mkdir(parents=True)
            (candidate / ".clawpatch-supervise-owned.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "owner": "clawpatch-supervise",
                        "kind": "run-temp",
                        "directory": candidate.name,
                        "pid": 999_999_999,
                        "created_unix": 0,
                    }
                ),
                encoding="utf-8",
            )
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    ["cleanup", "--dry-run"],
                    cleanup_root=cleanup_root,
                    ensure_repository_idle=lambda _repo: self.fail("preflight must not run"),
                    heartbeat_seconds=0,
                )

            self.assertEqual(code, 0)
            self.assertTrue(candidate.is_dir())
            self.assertIn("STALE", output.getvalue())
            self.assertIn(str(candidate), output.getvalue())
            self.assertIn("removed=0", output.getvalue())

    def test_cleanup_apply_removes_only_proven_stale_owned_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"
            stale = cleanup_root / "run-stale"
            active = cleanup_root / "run-active"
            recent = cleanup_root / "run-recent"
            unowned = cleanup_root / "run-unowned"
            outside = root / "outside"
            outside.mkdir()
            unowned.mkdir(parents=True)
            (cleanup_root / "run-link").symlink_to(outside, target_is_directory=True)
            self._mark(stale, pid=999_999_999, created_unix=0)
            self._mark(active, pid=os.getpid(), created_unix=0)
            self._mark(recent, pid=999_999_999, created_unix=time.time())
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    ["cleanup", "--apply"],
                    cleanup_root=cleanup_root,
                    ensure_repository_idle=lambda _repo: self.fail("preflight must not run"),
                    heartbeat_seconds=0,
                )

            self.assertEqual(code, 0)
            self.assertFalse(stale.exists())
            self.assertTrue(active.is_dir())
            self.assertTrue(recent.is_dir())
            self.assertTrue(unowned.is_dir())
            self.assertTrue((cleanup_root / "run-link").is_symlink())
            self.assertTrue(outside.is_dir())
            self.assertIn("ACTIVE", output.getvalue())
            self.assertIn("RECENT", output.getvalue())
            self.assertIn("UNOWNED", output.getvalue())
            self.assertIn("removed=1", output.getvalue())

    def test_supervisor_run_routes_temporary_files_into_owned_directory_then_removes_it(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            observed: dict[str, object] = {}

            @contextmanager
            def fake_validation_environment(repo: Path, *, progress, temporary_root: Path):
                observed["repo"] = repo
                observed["temporary_root"] = temporary_root
                observed["marker_exists"] = (
                    temporary_root.parent / ".clawpatch-supervise-owned.json"
                ).is_file()
                yield {"TEST_ONLY": "yes"}

            def fake_sweep(_repo: Path, **kwargs):
                observed["child_env"] = kwargs["child_env_overrides"]
                return {
                    "ok": True,
                    "finding_count": 0,
                    "open_findings": 0,
                    "git_head": "abc123",
                }

            with redirect_stdout(StringIO()):
                code = main(
                    ["--repo", temp, "--fresh"],
                    cleanup_root=cleanup_root,
                    run_sweep=fake_sweep,
                    provision_validation_environment=fake_validation_environment,
                    ensure_repository_idle=lambda _repo: None,
                    heartbeat_seconds=0,
                )

            self.assertEqual(code, 0)
            self.assertTrue(observed["marker_exists"])
            temporary_root = observed["temporary_root"]
            self.assertIsInstance(temporary_root, Path)
            child_env = observed["child_env"]
            self.assertIsInstance(child_env, dict)
            self.assertEqual(child_env["TEST_ONLY"], "yes")
            for variable in ("TMPDIR", "TMP", "TEMP"):
                self.assertEqual(child_env[variable], str(temporary_root))
            self.assertTrue(cleanup_root.is_dir())
            self.assertEqual(list(cleanup_root.iterdir()), [])

    @unittest.skipUnless(Path("/proc").is_dir(), "Linux live-reference proof")
    def test_cleanup_preserves_stale_owned_directory_referenced_by_live_child(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-orphaned-child"
            self._mark(candidate, pid=999_999_999, created_unix=0)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=candidate,
            )
            try:
                output = StringIO()
                with redirect_stdout(output):
                    code = main(
                        ["cleanup", "--apply"],
                        cleanup_root=cleanup_root,
                        heartbeat_seconds=0,
                    )

                self.assertEqual(code, 0)
                self.assertTrue(candidate.is_dir())
                self.assertIn("ACTIVE", output.getvalue())
                self.assertIn("removed=0", output.getvalue())
            finally:
                child.terminate()
                child.wait(timeout=5)

    def test_cleanup_without_proc_preserves_directory_referenced_by_live_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-live-reference"
            self._mark(candidate, pid=999_999_999, created_unix=0)
            output = StringIO()

            with (
                patch("clawpatch_supervise.cleanup._PROC_ROOT", Path(temp) / "missing-proc"),
                patch(
                    "clawpatch_supervise.cleanup._lsof_path_has_live_reference",
                    return_value=True,
                ) as lsof_probe,
                redirect_stdout(output),
            ):
                code = main(
                    ["cleanup", "--apply"],
                    cleanup_root=cleanup_root,
                    heartbeat_seconds=0,
                )

            self.assertEqual(code, 0)
            self.assertTrue(candidate.is_dir())
            self.assertIn("ACTIVE", output.getvalue())
            self.assertIn("removed=0", output.getvalue())
            lsof_probe.assert_called_once_with(candidate)

    def test_cleanup_without_proc_fails_closed_when_lsof_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-unproven"
            self._mark(candidate, pid=999_999_999, created_unix=0)
            output = StringIO()

            with (
                patch("clawpatch_supervise.cleanup._PROC_ROOT", Path(temp) / "missing-proc"),
                patch(
                    "clawpatch_supervise.cleanup._lsof_path_has_live_reference",
                    return_value=None,
                ) as lsof_probe,
                redirect_stdout(output),
            ):
                code = main(
                    ["cleanup", "--apply"],
                    cleanup_root=cleanup_root,
                    heartbeat_seconds=0,
                )

            self.assertEqual(code, 0)
            self.assertTrue(candidate.is_dir())
            self.assertIn("UNSAFE", output.getvalue())
            self.assertIn("removed=0", output.getvalue())
            lsof_probe.assert_called_once_with(candidate)

    def test_cleanup_refuses_symlinked_root_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            cleanup_root = root / "clawpatch-supervise-runs"
            cleanup_root.symlink_to(target, target_is_directory=True)
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    ["cleanup", "--apply"],
                    cleanup_root=cleanup_root,
                    heartbeat_seconds=0,
                )

            self.assertEqual(code, 2)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertIn("STOPPED", output.getvalue())
            self.assertIn("cannot be a symlink", output.getvalue())

    def test_windows_cleanup_liveness_check_never_signals_the_process(self) -> None:
        with (
            patch("clawpatch_supervise.cleanup.os.name", "nt"),
            patch(
                "clawpatch_supervise.cleanup.os.kill",
                side_effect=AssertionError("Windows cleanup must not signal a PID"),
            ),
            patch(
                "clawpatch_supervise.cleanup._windows_pid_is_running",
                return_value=True,
            ) as windows_probe,
        ):
            self.assertTrue(_pid_is_running(42))

        windows_probe.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
