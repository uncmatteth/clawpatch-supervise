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
from clawpatch_supervise import cleanup as cleanup_module
from clawpatch_supervise.cleanup import (
    _pid_is_running,
    cleanup_owned_runs,
    default_cleanup_root,
    owned_run_directory,
)
from clawpatch_supervise.errors import SafetyError


class CleanupCommandTests(unittest.TestCase):
    @staticmethod
    def _mark(candidate: Path, *, pid: int, created_unix: float) -> None:
        candidate.mkdir(mode=0o700, parents=True)
        candidate.parent.chmod(0o700)
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
            candidate.mkdir(mode=0o700, parents=True)
            cleanup_root.chmod(0o700)
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
            try:
                (cleanup_root / "run-link").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("directory symlink privilege is unavailable")
                raise
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

    def test_cleanup_apply_keeps_an_undeletable_owned_run_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            blocked = cleanup_root / "run-blocked"
            removable = cleanup_root / "run-removable"
            self._mark(blocked, pid=999_999_999, created_unix=0)
            self._mark(removable, pid=999_999_999, created_unix=0)
            original_remove = cleanup_module._remove_exact_owned_run

            def remove(candidate: Path, root: Path) -> None:
                if candidate == blocked:
                    raise PermissionError(5, "Access is denied", str(candidate / "node-compile-cache"))
                original_remove(candidate, root)

            with patch(
                "clawpatch_supervise.cleanup._remove_exact_owned_run",
                side_effect=remove,
            ):
                report = cleanup_owned_runs(
                    apply=True,
                    root=cleanup_root,
                    stale_after_seconds=0,
                )

            self.assertTrue(blocked.is_dir())
            self.assertFalse(removable.exists())
            self.assertEqual(report.removed, 1)
            self.assertEqual(
                {entry.path.name: entry.status for entry in report.entries},
                {"run-blocked": "BLOCKED", "run-removable": "STALE"},
            )

    def test_cleanup_apply_refuses_unowned_directory_replaced_after_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-replaced"
            original = cleanup_root / "run-original"
            self._mark(candidate, pid=999_999_999, created_unix=0)
            original_remove = cleanup_module._remove_exact_owned_run

            def replace_then_remove(path: Path, root: Path) -> None:
                path.rename(original)
                path.mkdir()
                (path / "keep.txt").write_text("replacement\n", encoding="utf-8")
                original_remove(path, root)

            with (
                patch(
                    "clawpatch_supervise.cleanup._remove_exact_owned_run",
                    side_effect=replace_then_remove,
                ),
                self.assertRaisesRegex(SafetyError, "failed its ownership check"),
            ):
                cleanup_owned_runs(
                    apply=True,
                    root=cleanup_root,
                    stale_after_seconds=0,
                )

            self.assertEqual(
                (candidate / "keep.txt").read_text(encoding="utf-8"),
                "replacement\n",
            )
            self.assertTrue(original.is_dir())

    def test_cleanup_apply_refuses_symlink_replaced_after_classification(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-replaced"
            original = cleanup_root / "run-original"
            outside = root / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("outside\n", encoding="utf-8")
            probe = root / "symlink-probe"
            try:
                probe.symlink_to(outside, target_is_directory=True)
                probe.unlink()
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("directory symlink privilege is unavailable")
                raise
            self._mark(candidate, pid=999_999_999, created_unix=0)
            original_remove = cleanup_module._remove_exact_owned_run

            def replace_then_remove(path: Path, cleanup_path: Path) -> None:
                path.rename(original)
                path.symlink_to(outside, target_is_directory=True)
                original_remove(path, cleanup_path)

            try:
                with (
                    patch(
                        "clawpatch_supervise.cleanup._remove_exact_owned_run",
                        side_effect=replace_then_remove,
                    ),
                    self.assertRaisesRegex(SafetyError, "no longer a safe directory"),
                ):
                    cleanup_owned_runs(
                        apply=True,
                        root=cleanup_root,
                        stale_after_seconds=0,
                    )
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("directory symlink privilege is unavailable")
                raise

            self.assertTrue(candidate.is_symlink())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside\n")
            self.assertTrue(original.is_dir())

    def test_cleanup_apply_preserves_replacement_created_during_final_liveness_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-replaced"
            original = cleanup_root / "run-original"
            self._mark(candidate, pid=999_999_999, created_unix=0)
            probes = 0

            def replace_during_final_probe(path: Path) -> bool:
                nonlocal probes
                probes += 1
                if probes == 2:
                    path.rename(original)
                    path.mkdir()
                    (path / "keep.txt").write_text("replacement\n", encoding="utf-8")
                return False

            with (
                patch(
                    "clawpatch_supervise.cleanup._path_has_live_reference",
                    side_effect=replace_during_final_probe,
                ),
                self.assertRaisesRegex(SafetyError, "changed during cleanup"),
            ):
                cleanup_owned_runs(apply=True, root=cleanup_root, stale_after_seconds=0)

            self.assertEqual(probes, 2)
            self.assertEqual(
                (candidate / "keep.txt").read_text(encoding="utf-8"),
                "replacement\n",
            )
            self.assertTrue(original.is_dir())

    def test_cleanup_refuses_nested_replacement_created_after_final_identity_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp) / "run-owned"
            nested = candidate / "nested"
            original = candidate / "nested-original"
            nested.mkdir(parents=True)
            candidate_metadata = candidate.lstat()
            original_stat = cleanup_module.os.stat
            replacement_identity: tuple[int, int] | None = None

            def replace_after_stat(path, *args, **kwargs):
                nonlocal replacement_identity
                result = original_stat(path, *args, **kwargs)
                if (
                    path == nested.name
                    and kwargs.get("dir_fd") is not None
                    and replacement_identity is None
                ):
                    nested.rename(original)
                    nested.mkdir()
                    metadata = nested.lstat()
                    replacement_identity = (metadata.st_dev, metadata.st_ino)
                return result

            with patch.object(
                cleanup_module.os,
                "stat",
                side_effect=replace_after_stat,
            ) as checked_stat:
                supported_dir_fd = {*cleanup_module.os.supports_dir_fd, checked_stat}
                with patch.object(
                    cleanup_module.os,
                    "supports_dir_fd",
                    supported_dir_fd,
                ), self.assertRaisesRegex(SafetyError, "changed during cleanup"):
                    cleanup_module._remove_exact_directory(
                        candidate,
                        (candidate_metadata.st_dev, candidate_metadata.st_ino),
                    )

            self.assertIsNotNone(replacement_identity)
            replacement_metadata = nested.lstat()
            self.assertEqual(
                (replacement_metadata.st_dev, replacement_metadata.st_ino),
                replacement_identity,
            )
            self.assertTrue(original.is_dir())

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
            self.assertEqual(child_env["NODE_DISABLE_COMPILE_CACHE"], "1")
            self.assertEqual(child_env["PYTHONUTF8"], "1")
            self.assertEqual(child_env["PYTHONIOENCODING"], "utf-8")
            self.assertTrue(cleanup_root.is_dir())
            self.assertEqual(list(cleanup_root.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX directory-relative cleanup")
    def test_repeated_owned_runs_remove_nested_directories_and_quarantines(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"

            for index in range(2):
                with owned_run_directory(root, root=cleanup_root) as owned_run:
                    nested = owned_run.temporary_root / "venv" / "lib" / f"run-{index}"
                    nested.mkdir(parents=True)
                    (nested / "artifact.pyc").write_bytes(b"compiled")

                self.assertEqual(list(cleanup_root.iterdir()), [])

    def test_owned_run_cleanup_succeeds_on_windows_without_directory_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"

            with (
                patch.object(cleanup_module.os, "name", "nt"),
                patch.object(cleanup_module.os, "supports_fd", set()),
                patch.object(cleanup_module.os, "supports_dir_fd", set()),
            ):
                with owned_run_directory(root, root=cleanup_root) as owned_run:
                    nested = owned_run.temporary_root / "venv" / "Lib" / "site-packages"
                    nested.mkdir(parents=True)
                    (nested / "artifact.pyc").write_bytes(b"compiled")

            self.assertEqual(list(cleanup_root.iterdir()), [])

    def test_failed_cleanup_restores_run_without_leaking_quarantine(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"
            retained: Path | None = None
            blocked: list[Path] = []
            original_remove = cleanup_module._remove_exact_directory

            def fail_run_removal(path: Path, identity: tuple[int, int]) -> None:
                if path.name.startswith("run-"):
                    raise PermissionError(5, "Access is denied", str(path))
                original_remove(path, identity)

            with patch(
                "clawpatch_supervise.cleanup._remove_exact_directory",
                side_effect=fail_run_removal,
            ):
                with owned_run_directory(
                    root,
                    root=cleanup_root,
                    on_blocked_cleanup=lambda path, _error: blocked.append(path),
                ) as owned_run:
                    retained = owned_run.path

            self.assertEqual(blocked, [retained])
            self.assertIsNotNone(retained)
            self.assertTrue(retained.is_dir())
            self.assertEqual(list(cleanup_root.iterdir()), [retained])

    def test_completed_run_warns_instead_of_stopping_when_windows_blocks_temp_cleanup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            cleanup_root = Path(temp) / "clawpatch-supervise-runs"
            output = StringIO()

            def fake_sweep(_repo: Path, **_kwargs):
                return {
                    "ok": True,
                    "finding_count": 36,
                    "open_findings": 0,
                    "git_head": "abc123",
                }

            original_remove = cleanup_module._remove_exact_owned_run
            calls = 0

            def blocked_final_remove(candidate: Path, root: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise PermissionError(5, "Access is denied", str(candidate / ".wrangler"))
                original_remove(candidate, root)

            with (
                patch(
                    "clawpatch_supervise.cleanup._remove_exact_owned_run",
                    side_effect=blocked_final_remove,
                ),
                redirect_stdout(output),
            ):
                code = main(
                    ["--repo", temp, "--fresh"],
                    cleanup_root=cleanup_root,
                    run_sweep=fake_sweep,
                    ensure_repository_idle=lambda _repo: None,
                    heartbeat_seconds=0,
                )

            self.assertEqual(code, 0)
            self.assertIn("WARNING: The operating system retained", output.getvalue())
            self.assertIn("COMPLETE", output.getvalue())
            self.assertNotIn("STOPPED", output.getvalue())

    def test_owned_run_directory_without_callback_surfaces_blocked_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"
            retained_path: Path | None = None

            with (
                patch(
                    "clawpatch_supervise.cleanup._remove_exact_owned_run",
                    side_effect=PermissionError(5, "Access is denied"),
                ),
                self.assertRaises(SafetyError) as raised,
            ):
                with owned_run_directory(root, root=cleanup_root) as owned_run:
                    retained_path = owned_run.path

            self.assertIsNotNone(retained_path)
            self.assertTrue(retained_path.is_dir())
            self.assertIn(str(retained_path), str(raised.exception))
            self.assertIsInstance(raised.exception.__cause__, PermissionError)

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

    @unittest.skipUnless(os.name == "posix", "POSIX lsof fallback")
    def test_lsof_no_match_ignores_unrelated_mount_warnings(self) -> None:
        result = subprocess.CompletedProcess(
            args=["lsof"],
            returncode=1,
            stdout="",
            stderr="lsof: WARNING: can't stat() an unrelated mount\n",
        )

        with (
            patch("clawpatch_supervise.cleanup.shutil.which", return_value="/usr/bin/lsof"),
            patch("clawpatch_supervise.cleanup.subprocess.run", return_value=result),
        ):
            self.assertFalse(cleanup_module._lsof_path_has_live_reference(Path("/candidate")))

    @unittest.skipUnless(os.name == "posix", "POSIX lsof fallback")
    def test_lsof_candidate_specific_error_is_inconclusive(self) -> None:
        candidate = Path("/candidate")
        result = subprocess.CompletedProcess(
            args=["lsof"],
            returncode=1,
            stdout="",
            stderr=f"lsof: WARNING: can't stat() {candidate}: Permission denied\n",
        )

        with (
            patch("clawpatch_supervise.cleanup.shutil.which", return_value="/usr/bin/lsof"),
            patch("clawpatch_supervise.cleanup.subprocess.run", return_value=result),
        ):
            self.assertIsNone(cleanup_module._lsof_path_has_live_reference(candidate))

    @unittest.skipUnless(os.name == "posix", "POSIX lsof fallback")
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

    @unittest.skipUnless(os.name == "posix", "POSIX lsof fallback")
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

    @unittest.skipUnless(os.name == "posix", "POSIX proc inspection")
    def test_cleanup_fails_closed_when_live_process_proc_links_are_inaccessible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"
            candidate = cleanup_root / "run-unproven"
            self._mark(candidate, pid=999_999_999, created_unix=0)
            proc_root = root / "proc"
            process = proc_root / "1234"
            descriptor_root = process / "fd"
            descriptor_root.mkdir(parents=True)
            original_iterdir = Path.iterdir
            original_resolve = Path.resolve

            def inspect_directory(path: Path):
                if path == descriptor_root:
                    raise PermissionError("descriptor inspection denied")
                return original_iterdir(path)

            def resolve_link(path: Path, *args, **kwargs):
                if path == process / "cwd":
                    raise PermissionError("cwd inspection denied")
                return original_resolve(path, *args, **kwargs)

            with (
                patch("clawpatch_supervise.cleanup._PROC_ROOT", proc_root),
                patch.object(Path, "iterdir", autospec=True, side_effect=inspect_directory),
                patch.object(Path, "resolve", autospec=True, side_effect=resolve_link),
                patch(
                    "clawpatch_supervise.cleanup._lsof_path_has_live_reference",
                    return_value=None,
                ) as lsof_probe,
                patch("clawpatch_supervise.cleanup.shutil.rmtree") as remove_tree,
            ):
                report = cleanup_owned_runs(
                    apply=True,
                    root=cleanup_root,
                    stale_after_seconds=0,
                )

            self.assertTrue(candidate.is_dir())
            self.assertEqual([entry.status for entry in report.entries], ["UNSAFE"])
            self.assertEqual(report.removed, 0)
            lsof_probe.assert_called_once_with(candidate)
            remove_tree.assert_not_called()

    def test_cleanup_refuses_symlinked_root_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            cleanup_root = root / "clawpatch-supervise-runs"
            try:
                cleanup_root.symlink_to(target, target_is_directory=True)
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("directory symlink privilege is unavailable")
                raise
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

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership and mode checks")
    def test_owned_run_refuses_preexisting_world_writable_cleanup_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"
            cleanup_root.mkdir()
            cleanup_root.chmod(0o777)

            with self.assertRaisesRegex(SafetyError, "group or world writable"):
                with owned_run_directory(root, root=cleanup_root):
                    self.fail("an unsafe cleanup root must not yield a run directory")

            self.assertEqual(list(cleanup_root.iterdir()), [])

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership and mode checks")
    def test_owned_run_refuses_unsafe_preexisting_default_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temporary_root = Path(temp)
            with (
                patch.dict(cleanup_module.os.environ, {"XDG_RUNTIME_DIR": ""}),
                patch("clawpatch_supervise.cleanup.tempfile.gettempdir", return_value=temp),
            ):
                cleanup_root = default_cleanup_root()
                cleanup_root.parent.mkdir()
                cleanup_root.parent.chmod(0o777)

                with self.assertRaisesRegex(SafetyError, "group or world writable"):
                    with owned_run_directory(temporary_root):
                        self.fail("an unsafe cleanup parent must not yield a run directory")

            self.assertFalse(cleanup_root.exists())

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership and mode checks")
    def test_default_cleanup_root_prefers_verified_per_user_runtime_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            runtime_root = Path(temp)
            runtime_root.chmod(0o700)

            with patch.dict(
                cleanup_module.os.environ,
                {"XDG_RUNTIME_DIR": str(runtime_root)},
            ):
                cleanup_root = default_cleanup_root()

            self.assertEqual(
                cleanup_root,
                runtime_root / "clawpatch-supervise" / "runs",
            )

    @unittest.skipUnless(hasattr(os, "getuid"), "POSIX ownership and mode checks")
    def test_owned_run_refuses_cleanup_root_owned_by_another_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cleanup_root = root / "clawpatch-supervise-runs"
            cleanup_root.mkdir()

            with (
                patch(
                    "clawpatch_supervise.cleanup.os.getuid",
                    return_value=os.getuid() + 1,
                ),
                self.assertRaisesRegex(SafetyError, "not owned by the current user"),
            ):
                with owned_run_directory(root, root=cleanup_root):
                    self.fail("a foreign-owned cleanup root must not yield a run directory")

            self.assertEqual(list(cleanup_root.iterdir()), [])

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
