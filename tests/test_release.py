from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clawpatch_supervise.clawpatch_protocol import (
    RepairAction,
    classify_clawpatch_failure,
)
from clawpatch_supervise.clawpatch_release import (
    _active_clawpatch_processes,
    _checkpoint_can_follow_supervisor_upgrade,
    _checkpoint_later_applied_attempt,
    _checkpoint_unapplied_attempt,
    _clawpatch_doctor,
    _clawpatch_version,
    _commit_attempt,
    _execute_fix,
    _external_state_home,
    _fix_command,
    _is_clawpatch_argv,
    _load_release_progress,
    _map_repository,
    _migrate_legacy_external_progress,
    _MissingFinding,
    _must_run,
    _must_clawpatch,
    _next_finding,
    _parse_json_output,
    _patch_attempt_from_show,
    _platform_command,
    _prepare_fresh_release,
    _process_finding_until_fixed,
    _publish_final_state,
    _push_and_verify,
    _rebuilt_generation_owns_checkpoint_source,
    _release_clawpatch_env,
    _repository_state_root,
    _require_synchronized_remote_branch,
    _resume_stopped_attempt,
    _revalidate,
    _review_all_features,
    _run_project_gates,
    _source_state_fingerprint,
    _status_entries,
    _source_paths,
    _source_paths_fingerprint,
    _UnresolvedFinding,
    _windows_clawpatch_processes,
    _windows_codex_sandbox_path,
    _write_release_progress,
    runtime_doctor,
    release_sweep,
)
from clawpatch_supervise.errors import GateFailure, RepositoryBusyError, SafetyError


def _hold_clawpatch_release_lock(repo: str, acquired, release) -> None:
    lock_path = Path(repo) / ".git" / "manageroo-clawpatch-release.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release Clawpatch sweep lock")
    finally:
        os.close(descriptor)


class ClawpatchReleaseSweepTests(unittest.TestCase):
    @staticmethod
    def completed(
        argv: list[str], output: str = "", code: int = 0
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, output, None)

    def test_must_run_redacts_failed_command_argv_and_output(self):
        with tempfile.TemporaryDirectory() as temp:
            argv = [
                sys.executable,
                "-c",
                "print('authorization: Bearer stdout-secret'); raise SystemExit(9)",
                "--token",
                "argument-secret",
            ]

            with self.assertRaises(SafetyError) as raised:
                _must_run(argv, cwd=Path(temp), timeout=30)

        message = str(raised.exception)
        self.assertNotIn("argument-secret", message)
        self.assertNotIn("stdout-secret", message)
        self.assertIn("<REDACTED>", message)
        self.assertIn("exit code: 9", message)

    def test_windows_codex_preflight_skips_broken_long_path_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repository"
            repo.mkdir()
            broken = root / "very-long-node-install"
            working = root / "short-npm-bin"
            broken.mkdir()
            working.mkdir()
            (broken / "codex.cmd").write_text("@echo off\r\n", encoding="ascii")
            (working / "codex.cmd").write_text("@echo off\r\n", encoding="ascii")
            path = f"{broken};{working}"
            failed = SimpleNamespace(passed=False, stdout="", stderr="path not found")
            passed = SimpleNamespace(
                passed=True,
                stdout="CLAWPATCH_WINDOWS_CODEX_SANDBOX_OK\n",
                stderr="",
            )

            with patch(
                "clawpatch_supervise.clawpatch_release.CommandRunner.run",
                side_effect=[failed, passed],
            ) as run:
                selected = _windows_codex_sandbox_path(
                    repo,
                    env={"PATH": path, "COMSPEC": "cmd.exe"},
                    platform_name="nt",
                )

        self.assertEqual(selected, f"{working};{path}")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(Path(run.call_args_list[0].args[0][0]), broken / "codex.cmd")
        self.assertEqual(Path(run.call_args_list[1].args[0][0]), working / "codex.cmd")

    def test_clawpatch_doctor_accepts_fresh_uninitialized_repository(self):
        output = json.dumps(
            {
                "state": "missing",
                "provider": "codex",
                "providerVersion": "test",
            }
        )
        completed = SimpleNamespace(passed=True, stdout=output, stderr="")
        with patch(
            "clawpatch_supervise.clawpatch_release.CommandRunner.run",
            return_value=completed,
        ):
            payload = _clawpatch_doctor(Path("repository"))

        self.assertEqual(payload["state"], "missing")
        self.assertEqual(payload["provider"], "codex")

    def test_windows_codex_preflight_stops_before_queue_when_every_launcher_is_broken(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tools = root / "node-install"
            tools.mkdir()
            (tools / "codex.cmd").write_text("@echo off\r\n", encoding="ascii")
            failed = SimpleNamespace(passed=False, stdout="", stderr="path not found")

            with (
                patch(
                    "clawpatch_supervise.clawpatch_release.CommandRunner.run",
                    return_value=failed,
                ),
                self.assertRaisesRegex(SafetyError, "No ClawPatch queue was started"),
            ):
                _windows_codex_sandbox_path(
                    root,
                    env={"PATH": str(tools), "COMSPEC": "cmd.exe"},
                    platform_name="nt",
                )

    def test_runtime_doctor_selects_working_codex_path_before_provider_probe(self):
        selected_path = r"C:\short-codex;C:\broken-codex"
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            with (
                patch.dict(
                    os.environ,
                    {"PATH": r"C:\broken-codex", "SYSTEMROOT": r"C:\Windows"},
                    clear=True,
                ),
                patch(
                    "clawpatch_supervise.clawpatch_release._git_root",
                    return_value=repo,
                ),
                patch(
                    "clawpatch_supervise.clawpatch_release._clawpatch_version",
                    return_value="0.7.2",
                ),
                patch(
                    "clawpatch_supervise.clawpatch_release._windows_codex_sandbox_path",
                    return_value=selected_path,
                ),
                patch(
                    "clawpatch_supervise.clawpatch_release._clawpatch_doctor",
                    return_value={"provider": "codex", "providerVersion": "test"},
                ) as doctor,
                patch(
                    "clawpatch_supervise.clawpatch_release._must_run",
                    return_value="git version test",
                ),
            ):
                report, overrides = runtime_doctor(repo)

        doctor_env = doctor.call_args.kwargs["env"]
        self.assertEqual(doctor_env["PATH"], selected_path)
        self.assertEqual(doctor_env["SYSTEMROOT"], r"C:\Windows")
        self.assertEqual(overrides, {"PATH": selected_path})
        self.assertEqual(report["windowsCodexSandbox"], "ready")

    @staticmethod
    def init_repo(repo: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
        )
        subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.hooksPath", "/dev/null"], cwd=repo, check=True)
        (repo / ".gitignore").write_text(".clawpatch/\n.manageroo/\n", encoding="utf-8")
        manageroo = repo / ".manageroo"
        manageroo.mkdir()
        (manageroo / "config.toml").write_text(
            "[safety]\n"
            'allowed_programs = ["git"]\n\n'
            "[[verification.gates]]\n"
            'id = "clean-baseline"\n'
            'kind = "test"\n'
            "required = true\n"
            "timeout_seconds = 60\n"
            'argv = ["git", "status", "--porcelain"]\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    @unittest.skipUnless(os.name == "posix", "POSIX byte filenames")
    def test_status_and_source_paths_preserve_distinct_non_utf8_filenames(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            names = [os.fsdecode(value) for value in (b"invalid-\xfe.txt", b"invalid-\xff.txt")]
            for name in names:
                (repo / name).write_text("original\n", encoding="utf-8")
            subprocess.run(["git", "add", "--", *names], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "byte paths"], cwd=repo, check=True)
            for name in names:
                (repo / name).write_text("changed\n", encoding="utf-8")

            entries = _status_entries(repo)

            self.assertEqual(entries, [(" M", name) for name in sorted(names)])
            self.assertEqual(_source_paths(repo), sorted(names))
            for _status, name in entries:
                self.assertTrue((repo / name).is_file())

    def test_source_paths_ignore_only_untracked_node_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            tracked_dependencies = [
                repo / "node_modules" / "vendored" / "source.js",
                repo / "packages" / "web" / "node_modules" / "vendored" / "source.js",
            ]
            for dependency in tracked_dependencies:
                dependency.parent.mkdir(parents=True)
                dependency.write_text("tracked v1\n", encoding="utf-8")
            subprocess.run(
                [
                    "git",
                    "add",
                    "node_modules/vendored/source.js",
                    "packages/web/node_modules/vendored/source.js",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-m", "tracked dependency"], cwd=repo, check=True
            )
            for dependency in tracked_dependencies:
                dependency.write_text("tracked v2\n", encoding="utf-8")
            installed_dependencies = [
                repo / "node_modules" / "installed" / "runtime.js",
                repo / "packages" / "web" / "node_modules" / "installed" / "runtime.js",
            ]
            for dependency in installed_dependencies:
                dependency.parent.mkdir(parents=True)
                dependency.write_text("untracked runtime\n", encoding="utf-8")
            near_match = repo / "packages" / "web" / "node_modules_backup" / "source.js"
            near_match.parent.mkdir(parents=True)
            near_match.write_text("untracked source\n", encoding="utf-8")
            (repo / "notes.txt").write_text("untracked source\n", encoding="utf-8")

            source_paths = _source_paths(repo)

        self.assertEqual(
            source_paths,
            [
                "node_modules/vendored/source.js",
                "notes.txt",
                "packages/web/node_modules/vendored/source.js",
                "packages/web/node_modules_backup/source.js",
            ],
        )

    @staticmethod
    def init_plain_repo(repo: Path) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True
        )
        subprocess.run(["git", "config", "commit.gpgSign", "false"], cwd=repo, check=True)
        subprocess.run(["git", "config", "core.hooksPath", "/dev/null"], cwd=repo, check=True)
        (repo / ".gitignore").write_text(".clawpatch/\n", encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    @staticmethod
    def add_submodule(repo: Path, root: Path, path: str = "lib/dependency") -> Path:
        dependency = root / "dependency"
        dependency.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=dependency, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=dependency, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=dependency,
            check=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgSign", "false"], cwd=dependency, check=True
        )
        subprocess.run(
            ["git", "config", "core.hooksPath", "/dev/null"], cwd=dependency, check=True
        )
        source = dependency / "source.py"
        source.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "source.py"], cwd=dependency, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "dependency"], cwd=dependency, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(dependency),
                path,
            ],
            cwd=repo,
            check=True,
        )
        cloned_dependency = repo / path
        subprocess.run(
            ["git", "config", "commit.gpgSign", "false"], cwd=cloned_dependency, check=True
        )
        subprocess.run(
            ["git", "config", "core.hooksPath", "/dev/null"], cwd=cloned_dependency, check=True
        )
        subprocess.run(["git", "commit", "-q", "-am", "add dependency"], cwd=repo, check=True)
        return cloned_dependency

    def test_source_fingerprint_hashes_untracked_symlink_without_following_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_plain_repo(repo)
            first_target = root / "release-one"
            second_target = root / "release-two"
            first_target.mkdir()
            second_target.mkdir()
            link = repo / "dist"
            try:
                link.symlink_to(first_target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            first = _source_state_fingerprint(repo)
            link.unlink()
            link.symlink_to(second_target, target_is_directory=True)
            second = _source_state_fingerprint(repo)

            self.assertEqual(first["paths"], ["dist"])
            self.assertEqual(set(first["untracked"]), {"dist"})
            self.assertTrue(first["untracked"]["dist"].startswith("symlink:"))
            self.assertNotEqual(first["untracked"]["dist"], second["untracked"]["dist"])

    def test_source_fingerprint_hashes_actual_dirty_submodule_content(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_plain_repo(repo)
            dependency = self.add_submodule(repo, root)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "config", "--local", "--get", "commit.gpgSign"],
                    cwd=dependency,
                    text=True,
                ).strip(),
                "false",
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "config", "--local", "--get", "core.hooksPath"],
                    cwd=dependency,
                    text=True,
                ).strip(),
                "/dev/null",
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=dependency, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"],
                cwd=dependency,
                check=True,
            )
            leaf_source = root / "leaf"
            leaf_source.mkdir()
            self.init_plain_repo(leaf_source)
            (leaf_source / "source.py").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "source.py"], cwd=leaf_source, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "leaf"], cwd=leaf_source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    "-q",
                    str(leaf_source),
                    "lib/leaf",
                ],
                cwd=dependency,
                check=True,
            )
            subprocess.run(
                ["git", "commit", "-q", "-am", "add nested dependency"],
                cwd=dependency,
                check=True,
            )
            subprocess.run(["git", "add", "lib/dependency"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "update dependency"], cwd=repo, check=True)

            leaf = dependency / "lib" / "leaf"
            source = leaf / "source.py"
            source.write_text("first repair\n", encoding="utf-8")
            (leaf / "new_test.py").write_text("first test\n", encoding="utf-8")
            first = _source_state_fingerprint(repo)

            source.write_text("different repair\n", encoding="utf-8")
            (leaf / "new_test.py").write_text("different test\n", encoding="utf-8")
            second = _source_state_fingerprint(repo)

            self.assertEqual(first["paths"], ["lib/dependency"])
            self.assertEqual(set(first["gitlinks"]), {"lib/dependency"})
            dependency_state = first["gitlinks"]["lib/dependency"]
            self.assertEqual(dependency_state["paths"], ["lib/leaf"])
            self.assertEqual(
                set(dependency_state["gitlinks"]["lib/leaf"]["untracked"]),
                {"new_test.py"},
            )
            self.assertNotEqual(first["gitlinks"], second["gitlinks"])

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_external_sweep_runs_in_plain_git_repo_without_manageroo_files(
        self,
        _version,
        _processes,
        json_clawpatch,
        review_all,
        next_finding,
        show_finding,
        execute_fix,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            manageroo_state = root / "manageroo-owned-state"
            self.init_plain_repo(repo)
            json_clawpatch.side_effect = [
                {"created": True, "next": "clawpatch map"},
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 1},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            next_finding.side_effect = [
                ("fnd_one", {"finding": {"id": "fnd_one", "status": "open"}}),
                (None, {"finding": None}),
            ]
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "patchAttempts": [],
            }

            def complete_fix(*_args, **_kwargs):
                self.assertFalse((repo / ".manageroo").exists())
                self.assertEqual(
                    subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True),
                    "",
                )
                (repo / "app.py").write_text("fixed\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = complete_fix
            final_closure.return_value = {"pushed": False}

            with patch(
                "clawpatch_supervise.clawpatch_release._external_state_home",
                return_value=manageroo_state,
                create=True,
            ):
                report = release_sweep(
                    repo,
                    apply=True,
                    branch="current",
                    integration_mode="external",
                )

            proof_path = Path(report["proof_path"])
            self.assertIn(manageroo_state.resolve(), proof_path.parents)
            self.assertTrue(proof_path.is_file())
            self.assertFalse((repo / ".manageroo").exists())
            self.assertFalse((repo / ".git" / "manageroo").exists())
            self.assertEqual(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True),
                "",
            )
            self.assertEqual(
                [invocation.args[1] for invocation in json_clawpatch.call_args_list],
                [
                    ["clawpatch", "init", "--json"],
                    ["clawpatch", "status", "--json"],
                    ["clawpatch", "map", "--json"],
                ],
            )

    @patch("clawpatch_supervise.clawpatch_release.sys.base_prefix", "/usr")
    @patch(
        "clawpatch_supervise.clawpatch_release.sys.prefix",
        "/home/test/.local/share/clawpatch-supervise/venv",
    )
    @unittest.skipIf(os.name == "nt", "POSIX state home only")
    def test_external_state_home_is_stable_across_python_environments(self):
        with patch.dict(os.environ, {"XDG_STATE_HOME": "/home/test/.local/state"}):
            self.assertEqual(
                _external_state_home(),
                Path("/home/test/.local/state/clawpatch-supervise"),
            )

    @unittest.skipUnless(os.name == "nt", "Windows state home only")
    def test_windows_external_state_home_uses_local_app_data(self):
        local_app_data = r"C:\Users\Test\AppData\Local"
        with patch.dict(os.environ, {"LOCALAPPDATA": local_app_data}):
            self.assertEqual(
                _external_state_home(),
                Path(local_app_data) / "ClawPatchSupervise" / "state",
            )

    def test_external_progress_migrates_from_the_old_venv_state_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_repo(repo)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            legacy_home = root / "old-venv-state"
            legacy_root = _repository_state_root(legacy_home, repo)
            current_root = root / "canonical" / "repositories" / "current"
            expected = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                state_root=legacy_root,
            )

            with patch(
                "clawpatch_supervise.clawpatch_release._legacy_external_state_homes",
                return_value=(legacy_home,),
            ):
                _migrate_legacy_external_progress(repo, state_root=current_root)

            self.assertEqual(_load_release_progress(repo, state_root=current_root), expected)
            self.assertFalse((legacy_root / "clawpatch-release-progress.json").exists())

    def test_external_progress_upgrades_a_verified_legacy_source_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("interrupted repair\n", encoding="utf-8")

            legacy_home = root / "manageroo-state"
            legacy_root = _repository_state_root(legacy_home, repo)
            current_root = root / "canonical" / "repositories" / "current"
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
                state_root=legacy_root,
            )
            modern_fingerprint = checkpoint["owned_source_fingerprint"]
            legacy_state = _source_state_fingerprint(repo)
            legacy_state.pop("gitlinks")
            legacy_fingerprint = hashlib.sha256(
                json.dumps(
                    legacy_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.assertNotEqual(legacy_fingerprint, modern_fingerprint)

            progress_path = legacy_root / "clawpatch-release-progress.json"
            checkpoint["version"] = 4
            checkpoint["owned_source_fingerprint"] = legacy_fingerprint
            progress_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            with patch(
                "clawpatch_supervise.clawpatch_release._legacy_external_state_homes",
                return_value=(legacy_home,),
            ):
                _migrate_legacy_external_progress(repo, state_root=current_root)

            migrated = _load_release_progress(repo, state_root=current_root)
            self.assertEqual(migrated["owned_source_fingerprint"], modern_fingerprint)
            self.assertFalse(progress_path.exists())

    def test_external_progress_upgrades_canonical_legacy_fingerprint_only_after_verification(
        self,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("interrupted repair\n", encoding="utf-8")

            current_root = root / "canonical" / "repositories" / "current"
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
                state_root=current_root,
            )
            modern_fingerprint = checkpoint["owned_source_fingerprint"]
            legacy_state = _source_state_fingerprint(repo)
            legacy_state.pop("gitlinks")
            checkpoint["version"] = 4
            legacy_fingerprint = hashlib.sha256(
                json.dumps(
                    legacy_state,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            checkpoint["owned_source_fingerprint"] = legacy_fingerprint
            progress_path = current_root / "clawpatch-release-progress.json"
            progress_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            source.write_text("later unrelated change\n", encoding="utf-8")
            with patch(
                "clawpatch_supervise.clawpatch_release._legacy_external_state_homes",
                return_value=(),
            ):
                _migrate_legacy_external_progress(repo, state_root=current_root)

            preserved = _load_release_progress(repo, state_root=current_root)
            self.assertEqual(preserved["version"], 4)
            self.assertEqual(preserved["owned_source_fingerprint"], legacy_fingerprint)

            source.write_text("interrupted repair\n", encoding="utf-8")
            with patch(
                "clawpatch_supervise.clawpatch_release._legacy_external_state_homes",
                return_value=(),
            ):
                _migrate_legacy_external_progress(repo, state_root=current_root)

            migrated = _load_release_progress(repo, state_root=current_root)
            self.assertEqual(migrated["version"], 6)
            self.assertEqual(migrated["owned_source_fingerprint"], modern_fingerprint)

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_external_fresh_migrates_fingerprinted_checkpoint_from_legacy_state_location(
        self,
        _version,
        _processes,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("interrupted ClawPatch repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            manageroo_state = root / "manageroo-owned-state"
            json_clawpatch.side_effect = [
                {"created": True},
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 0},
            ]
            review_all.return_value = {
                "review": {"reviewed": 0, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            with patch(
                "clawpatch_supervise.clawpatch_release._external_state_home",
                return_value=manageroo_state,
            ):
                with self.assertRaisesRegex(
                    SafetyError,
                    "fresh Clawpatch reset is allowed only when project source is clean",
                ):
                    release_sweep(
                        repo,
                        apply=True,
                        branch="current",
                        fresh=True,
                        integration_mode="external",
                    )

            self.assertEqual(source.read_text(encoding="utf-8"), "interrupted ClawPatch repair\n")
            self.assertIsNotNone(
                next(manageroo_state.rglob("clawpatch-release-progress.json"), None)
            )

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_external_fresh_refuses_current_source_changes_without_checkpoint(
        self,
        _version,
        _processes,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_plain_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("preserve this external fresh work\n", encoding="utf-8")
            source_before = source.read_bytes()
            stale = repo / ".clawpatch" / "findings" / "stale.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("{}\n", encoding="utf-8")
            manageroo_state = root / "manageroo-owned-state"
            json_clawpatch.side_effect = [
                {"created": True},
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 0},
            ]
            review_all.return_value = {
                "review": {"reviewed": 0, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            with patch(
                "clawpatch_supervise.clawpatch_release._external_state_home",
                return_value=manageroo_state,
            ):
                with self.assertRaisesRegex(
                    SafetyError,
                    "fresh Clawpatch reset is allowed only when project source is clean",
                ):
                    release_sweep(
                        repo,
                        apply=True,
                        branch="current",
                        fresh=True,
                        integration_mode="external",
                    )

            self.assertEqual(source.read_bytes(), source_before)
            self.assertTrue(stale.exists())

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_fresh_refuses_legacy_checkpoint_without_exact_source_provenance(
        self, json_clawpatch, _processes
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch="main",
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            checkpoint["version"] = 3
            checkpoint.pop("owned_source_fingerprint")
            progress_path = repo / ".manageroo/cache/clawpatch-release-progress.json"
            progress_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            source.write_text("manual operator edit\n", encoding="utf-8")
            source_before = source.read_bytes()

            with self.assertRaisesRegex(
                SafetyError,
                "fresh Clawpatch reset is allowed only when project source is clean",
            ):
                _prepare_fresh_release(repo, env={"PATH": "test"})

            self.assertEqual(source.read_bytes(), source_before)
            self.assertTrue(progress_path.is_file())
            json_clawpatch.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release.shutil.which", return_value="/usr/bin/clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._must_run")
    def test_clawpatch_release_sweep_requires_072_or_newer(self, must_run, _which):
        must_run.return_value = "0.7.1\n"
        with self.assertRaisesRegex(SafetyError, "0.7.2 or newer"):
            _clawpatch_version(Path("/repo"))

        must_run.return_value = "0.7.2\n"
        self.assertEqual(_clawpatch_version(Path("/repo")), "0.7.2")

    def test_json_parser_accepts_clawpatch_payload_before_progress_lines(self):
        output = (
            '{"features":35,"new":1,"changed":7,"stale":0,'
            '"source":"heuristic","usedAgent":false,'
            '"reason":"heuristic mapper selected","next":"clawpatch review --limit 3"}\n'
            "clawpatch map start source=heuristic existing=34 dryRun=false\n"
            "clawpatch map mapper-start mapper=python\n"
            "clawpatch map done features=35 usedAgent=false elapsed=0s\n"
        )

        payload = _parse_json_output(output, command="map --json")

        self.assertEqual(payload["features"], 35)
        self.assertEqual(payload["next"], "clawpatch review --limit 3")

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_zero_feature_heuristic_map_escalates_to_clawpatch_agent_mapper(self, json_clawpatch):
        progress_events = []
        json_clawpatch.side_effect = [
            {
                "features": 0,
                "new": 0,
                "changed": 0,
                "stale": 0,
                "source": "heuristic",
                "usedAgent": False,
                "reason": "heuristic mapper selected",
            },
            {
                "features": 7,
                "new": 7,
                "changed": 0,
                "stale": 0,
                "source": "agent",
                "usedAgent": True,
                "reason": "heuristic mapper produced no features",
            },
        ]

        mapped = _map_repository(Path("/repo"), env={}, progress=progress_events.append)

        self.assertEqual(mapped["features"], 7)
        self.assertEqual(
            [call.args[1] for call in json_clawpatch.call_args_list],
            [
                ["clawpatch", "map", "--json"],
                ["clawpatch", "map", "--source", "agent", "--json"],
            ],
        )
        self.assertEqual(json_clawpatch.call_args_list[1].kwargs["phase"], "map-agent")
        self.assertEqual(progress_events, [])

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_fresh_run_deletes_only_old_clawpatch_state_and_preserves_committed_config(
        self, json_clawpatch, _processes
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            state = repo / ".clawpatch"
            (state / "findings").mkdir(parents=True)
            config_text = '{"schemaVersion":1,"commands":{"test":"npm run test"}}\n'
            (state / "config.json").write_text(config_text, encoding="utf-8")
            (state / "findings" / "stale.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", ".clawpatch/config.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "config"], cwd=repo, check=True)
            checkpoint = repo / ".manageroo/cache/clawpatch-release-progress.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text("{}\n", encoding="utf-8")

            def initialize(*_args, **_kwargs):
                self.assertFalse(state.exists())
                state.mkdir()
                (state / "config.json").write_text('{"detected":true}\n', encoding="utf-8")
                return {"created": True}

            json_clawpatch.side_effect = initialize
            _prepare_fresh_release(repo, env={"PATH": "test"})

            self.assertFalse((state / "findings" / "stale.json").exists())
            self.assertFalse(checkpoint.exists())
            self.assertEqual((state / "config.json").read_text(encoding="utf-8"), config_text)
            self.assertEqual(
                json_clawpatch.call_args.args[1],
                ["clawpatch", "init", "--json"],
            )

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_fresh_run_excludes_git_submodules_from_clawpatch_mapping(
        self, json_clawpatch, _processes
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_plain_repo(repo)
            self.add_submodule(repo, root)
            state = repo / ".clawpatch"
            state.mkdir()
            (state / "config.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "include": ["**/*"],
                        "exclude": ["node_modules/**"],
                    }
                ),
                encoding="utf-8",
            )

            def initialize(*_args, **_kwargs):
                state.mkdir()
                (state / "config.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "include": ["**/*"],
                            "exclude": ["node_modules/**"],
                        }
                    ),
                    encoding="utf-8",
                )
                return {"created": True}

            json_clawpatch.side_effect = initialize
            _prepare_fresh_release(repo, env={"PATH": "test"})

            config = json.loads((state / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(
                config["exclude"],
                ["node_modules/**", "lib/dependency", "lib/dependency/**"],
            )

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_fresh_run_preserves_checkpoint_owned_interrupted_source(
        self, json_clawpatch, _processes
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            unrelated = repo / "notes.txt"
            source.write_text("original\n", encoding="utf-8")
            unrelated.write_text("original notes\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "notes.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)

            finding_id = "fnd_sig-feat-library-abc123-1234_abcdef1234"
            state = repo / ".clawpatch"
            finding_path = state / "findings" / f"{finding_id}.json"
            finding_path.parent.mkdir(parents=True)
            finding_path.write_text(
                json.dumps(
                    {
                        "findingId": finding_id,
                        "status": "open",
                        "evidence": [{"path": "app.py"}],
                    }
                ),
                encoding="utf-8",
            )
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("interrupted Clawpatch repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id=finding_id,
                branch="main",
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
            )

            def initialize(*_args, **_kwargs):
                state.mkdir()
                return {"created": True}

            json_clawpatch.side_effect = initialize
            with self.assertRaisesRegex(
                SafetyError,
                "fresh Clawpatch reset is allowed only when project source is clean",
            ):
                _prepare_fresh_release(repo, env={"PATH": "test"})

            self.assertEqual(source.read_text(encoding="utf-8"), "interrupted Clawpatch repair\n")
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "original notes\n")
            self.assertEqual(
                subprocess.check_output(["git", "stash", "list"], cwd=repo, text=True),
                "",
            )
            self.assertTrue((repo / ".manageroo/cache/clawpatch-release-progress.json").exists())

            unrelated.write_text("operator work\n", encoding="utf-8")
            with self.assertRaisesRegex(SafetyError, "project source is clean"):
                _prepare_fresh_release(repo, env={"PATH": "test"})
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "operator work\n")

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_fresh_run_retires_source_clean_legacy_temporary_commit_checkpoint(
        self, json_clawpatch, _processes
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            test_source = repo / "test_app.py"
            source.write_text("original\n", encoding="utf-8")
            test_source.write_text("original test\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "test_app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            finding_id = "fnd_sig-feat-library-abc123-1234_abcdef1234"
            source.write_text("partial repair\n", encoding="utf-8")
            test_source.write_text("partial test repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "test_app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    f"clawpatch-supervise iteration: {finding_id}",
                ],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            temporary_tree = subprocess.check_output(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            subprocess.run(["git", "restore", "--", "app.py", "test_app.py"], cwd=repo, check=True)

            checkpoint = _write_release_progress(
                repo,
                finding_id=finding_id,
                branch="main",
                head_before=original_head,
                phase="stopped",
                owned_paths=[],
                temporary_commit=temporary_commit,
                source_states=[
                    subprocess.check_output(
                        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
                    ).strip(),
                    temporary_tree,
                ],
                last_action="stop-terminal",
            )
            checkpoint["version"] = 5
            checkpoint["owned_source_fingerprint"] = ""
            progress_path = repo / ".manageroo/cache/clawpatch-release-progress.json"
            progress_path.write_text(json.dumps(checkpoint), encoding="utf-8")
            state = repo / ".clawpatch"
            state.mkdir()

            def initialize(*_args, **_kwargs):
                state.mkdir()
                return {"created": True}

            json_clawpatch.side_effect = initialize
            _prepare_fresh_release(repo, env={"PATH": "test"})

            self.assertFalse(progress_path.exists())
            self.assertEqual(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True),
                "",
            )
            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
                original_head,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", temporary_commit], cwd=repo, text=True
                ).strip(),
                temporary_commit,
            )

    def test_explicit_state_publication_commits_new_safe_clawpatch_state(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            (repo / ".gitignore").write_text(".manageroo/\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "track clawpatch state"], cwd=repo, check=True
            )
            feature = repo / ".clawpatch" / "features" / "feat_one.json"
            feature.parent.mkdir(parents=True)
            feature.write_text('{"featureId":"feat_one"}\n', encoding="utf-8")

            commit = _publish_final_state(repo, branch="main")

            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            self.assertEqual(commit, head)
            committed = subprocess.check_output(
                ["git", "show", "--pretty=", "--name-only", "HEAD"], cwd=repo, text=True
            ).splitlines()
            self.assertEqual(committed, [".clawpatch/features/feat_one.json"])
            self.assertEqual(
                subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True), ""
            )

    @patch("clawpatch_supervise.clawpatch_release._require_branch")
    @patch("clawpatch_supervise.clawpatch_release._validate_attempt_paths")
    @patch("clawpatch_supervise.clawpatch_release._source_paths")
    @patch("clawpatch_supervise.clawpatch_release._must_run")
    def test_commit_accepts_reported_file_that_normalizes_to_no_staged_diff(
        self, must_run, source_paths, validate_attempt_paths, _require_branch
    ):
        files = ["package.json", "test/access.test.js", "test/package-entry.test.js"]
        source_paths.return_value = ["package.json", "test/package-entry.test.js"]
        must_run.side_effect = [
            "",
            "package.json\0test/package-entry.test.js\0",
            "",
            "",
            "abc123",
            "package.json\ntest/package-entry.test.js\n",
        ]

        commit = _commit_attempt(Path("C:/repo"), "fnd_one", files, branch="master")

        self.assertEqual(commit, "abc123")
        validate_attempt_paths.assert_called_once_with(Path("C:/repo"), files)
        self.assertEqual(
            must_run.call_args_list[0].args[0],
            ["git", "add", "--", *files],
        )

    @patch.dict(
        "clawpatch_supervise.clawpatch_release.os.environ",
        {"CLAWPATCH_CODEX_SANDBOX": "bypass"},
    )
    def test_release_environment_requires_explicit_authorization_for_sandbox_bypass(self):
        unauthorized = _release_clawpatch_env(trusted_host_codex_sandbox_bypass=False)
        fallback = _release_clawpatch_env(
            trusted_host_codex_sandbox_bypass=False,
            allow_sandbox_bypass_fallback=True,
        )
        authorized = _release_clawpatch_env(trusted_host_codex_sandbox_bypass=True)

        self.assertNotIn("CLAWPATCH_CODEX_SANDBOX", unauthorized)
        self.assertNotIn("CLAWPATCH_CODEX_SANDBOX", fallback)
        self.assertEqual(fallback["MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK"], "1")
        self.assertEqual(authorized["CLAWPATCH_CODEX_SANDBOX"], "bypass")

    def test_release_environment_scopes_validated_service_variables_to_children(self):
        child = _release_clawpatch_env(
            trusted_host_codex_sandbox_bypass=False,
            child_env_overrides={
                "TEST_DATABASE_URL": "postgresql://127.0.0.1:49152/test",
                "BTT_ALLOW_DATABASE_RESET": "true",
            },
        )

        self.assertEqual(
            child["TEST_DATABASE_URL"],
            "postgresql://127.0.0.1:49152/test",
        )
        self.assertEqual(child["BTT_ALLOW_DATABASE_RESET"], "true")

    @patch.dict(
        "clawpatch_supervise.clawpatch_release.os.environ",
        {
            "PRODUCTION_DATABASE_URL": "postgresql://production.invalid/live",
            "SECONDARY_DB_PASSWORD": "do-not-inherit",
            "PGHOST": "production.invalid",
            "MYSQL_PWD": "do-not-inherit",
            "MYSQL_ROOT_PASSWORD": "do-not-inherit",
            "PRODUCTION_ALLOW_DATABASE_RESET": "true",
            "SAFE_VALUE": "do-not-inherit",
        },
        clear=True,
    )
    def test_database_validation_removes_unrelated_database_credentials(self):
        child = _release_clawpatch_env(
            trusted_host_codex_sandbox_bypass=False,
            child_env_overrides={
                "TEST_DATABASE_URL": "postgresql://127.0.0.1:49152/test",
                "BTT_ALLOW_DATABASE_RESET": "true",
            },
        )

        self.assertNotIn("SAFE_VALUE", child)
        self.assertEqual(child["BTT_ALLOW_DATABASE_RESET"], "true")
        self.assertNotIn("PRODUCTION_DATABASE_URL", child)
        self.assertNotIn("SECONDARY_DB_PASSWORD", child)
        self.assertNotIn("PGHOST", child)
        self.assertNotIn("MYSQL_PWD", child)
        self.assertNotIn("MYSQL_ROOT_PASSWORD", child)
        self.assertNotIn("PRODUCTION_ALLOW_DATABASE_RESET", child)

    @patch.dict(
        "clawpatch_supervise.clawpatch_release.os.environ",
        {
            "TEST_DATABASE_URL": "postgresql://external.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "true",
            "SAFE_VALUE": "do-not-inherit",
        },
        clear=True,
    )
    def test_release_environment_removes_inherited_reset_capable_database(self):
        child = _release_clawpatch_env(trusted_host_codex_sandbox_bypass=False)

        self.assertNotIn("SAFE_VALUE", child)
        self.assertNotIn("TEST_DATABASE_URL", child)
        self.assertNotIn("BTT_ALLOW_DATABASE_RESET", child)

    def test_release_environment_rejects_validation_service_policy_overrides(self):
        for name in (
            "CLAWPATCH_CODEX_SANDBOX",
            "CLAWPATCH_CODEX_TIMEOUT_MS",
            "MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK",
            "MANAGEROO_CLAWPATCH_CHILD_TIMEOUT_SECONDS",
            "clawpatch_codex_sandbox",
        ):
            with self.subTest(name=name):
                with self.assertRaisesRegex(SafetyError, "policy-owned"):
                    _release_clawpatch_env(
                        trusted_host_codex_sandbox_bypass=False,
                        child_env_overrides={name: "bypass"},
                    )

    def test_process_matcher_ignores_clawpatch_mentions_inside_gbrain_context(self):
        gbrain = [
            "bun",
            "/home/Tommy/.bun/bin/gbrain",
            "call",
            "volunteer_context",
            '{"window":"assistant: run clawpatch fix --finding fnd_one"}',
        ]

        self.assertFalse(_is_clawpatch_argv(gbrain))
        self.assertTrue(
            _is_clawpatch_argv(
                ["node", "/home/Tommy/.local/bin/clawpatch", "fix", "--finding", "fnd_one"]
            )
        )
        self.assertTrue(
            _is_clawpatch_argv(
                ["python", "/home/Tommy/.local/bin/clawpatch-supervise", "--repo", "."]
            )
        )

    def test_process_matcher_recognizes_supported_python_invocations(self):
        invocations = (
            ["python", "-m", "clawpatch_supervise"],
            ["python", "-u", "-m", "clawpatch_supervise"],
            ["python", "-X", "utf8", "-m", "clawpatch_supervise"],
            ["python", "-I", "-u", "-X", "utf8", "-m", "clawpatch_supervise"],
            ["python.exe", "-u", "-Xutf8", "-m", "clawpatch_supervise"],
            ["python3", "-u", "/home/Tommy/.local/bin/clawpatch-supervise", "--repo", "."],
        )

        for argv in invocations:
            with self.subTest(argv=argv):
                self.assertTrue(_is_clawpatch_argv(argv))

        self.assertFalse(
            _is_clawpatch_argv(
                ["python", "unrelated.py", "-m", "clawpatch_supervise"]
            )
        )

    @unittest.skipIf(os.name == "nt", "POSIX process inventory only")
    @patch("clawpatch_supervise.clawpatch_release.Path.is_dir", return_value=False)
    @patch("clawpatch_supervise.clawpatch_release.os.getpid", return_value=101)
    @patch("clawpatch_supervise.clawpatch_release.shutil.which", return_value="/usr/bin/lsof")
    @patch("clawpatch_supervise.clawpatch_release._run")
    def test_unix_process_inventory_matches_root_and_subdirectory_not_other_repositories(
        self, run, _which, _getpid, _is_dir
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            subdir = repo / "subdir"
            other = root / "other"
            subdir.mkdir(parents=True)
            other.mkdir()
            repo = repo.resolve()
            subdir = subdir.resolve()
            other = other.resolve()

            def inspect(argv, **_kwargs):
                if argv[0] == "ps":
                    return self.completed(
                        argv,
                        f"101 python3 -m clawpatch_supervise.clawpatch_external --repo {repo}\n"
                        f"202 clawpatch-supervise --repo {other}\n"
                        "303 clawpatch map\n"
                        "404 clawpatch fix --finding fnd_one\n"
                        f"505 python3 -u -X utf8 -m clawpatch_supervise --repo {repo}\n",
                    )
                if argv[0] == "git":
                    cwd = Path(_kwargs["cwd"]).resolve()
                    git_root = other if cwd == other else repo
                    return self.completed(argv, f"{git_root}\n")
                pid = argv[argv.index("-p") + 1]
                cwd = other if pid == "202" else subdir if pid == "303" else repo
                return self.completed(argv, f"p{pid}\nfcwd\nn{cwd}\n")

            run.side_effect = inspect

            self.assertEqual(
                _active_clawpatch_processes(repo),
                [
                    {"pid": 303, "cwd": str(subdir), "command": "clawpatch map"},
                    {
                        "pid": 404,
                        "cwd": str(repo),
                        "command": "clawpatch fix --finding fnd_one",
                    },
                    {
                        "pid": 505,
                        "cwd": str(repo),
                        "command": (
                            f"python3 -u -X utf8 -m clawpatch_supervise --repo {repo}"
                        ),
                    },
                ],
            )

    def test_pushable_branch_accepts_a_clean_local_ahead_history_before_fixing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            remote = root / "remote.git"
            repo.mkdir()
            self.init_repo(repo)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            self.assertEqual(_require_synchronized_remote_branch(repo, branch), head)

            (repo / "local-only.txt").write_text("ahead\n", encoding="utf-8")
            subprocess.run(["git", "add", "local-only.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "local only"], cwd=repo, check=True)
            local_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            self.assertEqual(
                _require_synchronized_remote_branch(repo, branch),
                local_head,
            )

    def test_pushable_branch_merges_clean_divergent_histories_before_fixing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            publisher = root / "publisher"
            remote = root / "remote.git"
            repo.mkdir()
            self.init_repo(repo)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True)

            (repo / "local-only.txt").write_text("local\n", encoding="utf-8")
            subprocess.run(["git", "add", "local-only.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "local only"], cwd=repo, check=True)
            local_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            subprocess.run(
                ["git", "clone", "-q", "--branch", branch, str(remote), str(publisher)],
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=publisher, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=publisher,
                check=True,
            )
            (publisher / "remote-only.txt").write_text("remote\n", encoding="utf-8")
            subprocess.run(["git", "add", "remote-only.txt"], cwd=publisher, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "remote only"], cwd=publisher, check=True)
            subprocess.run(["git", "push", "-q", "origin", branch], cwd=publisher, check=True)
            remote_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=publisher, text=True
            ).strip()
            events = []

            merged = _require_synchronized_remote_branch(repo, branch, progress=events.append)

            self.assertNotEqual(merged, local_head)
            for ancestor in (local_head, remote_head):
                result = subprocess.run(
                    ["git", "merge-base", "--is-ancestor", ancestor, merged],
                    cwd=repo,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=repo, text=True
                ),
                "",
            )
            self.assertTrue(any(event.get("phase") == "git-sync" for event in events))

    def test_dirty_remote_mismatch_waits_instead_of_stopping_terminally(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            publisher = root / "publisher"
            remote = root / "remote.git"
            repo.mkdir()
            self.init_repo(repo)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True)
            subprocess.run(
                ["git", "clone", "-q", "--branch", branch, str(remote), str(publisher)],
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=publisher, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=publisher,
                check=True,
            )
            (publisher / "remote.txt").write_text("remote\n", encoding="utf-8")
            subprocess.run(["git", "add", "remote.txt"], cwd=publisher, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "remote"], cwd=publisher, check=True)
            subprocess.run(["git", "push", "-q", "origin", branch], cwd=publisher, check=True)
            (repo / "dirty.txt").write_text("preserve me\n", encoding="utf-8")

            with self.assertRaises(RepositoryBusyError):
                _require_synchronized_remote_branch(repo, branch)

            self.assertEqual((repo / "dirty.txt").read_text(encoding="utf-8"), "preserve me\n")

    def test_conflicting_divergent_histories_restore_exact_tree_and_wait(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            publisher = root / "publisher"
            remote = root / "remote.git"
            repo.mkdir()
            self.init_repo(repo)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True)
            subprocess.run(
                ["git", "clone", "-q", "--branch", branch, str(remote), str(publisher)],
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=publisher, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=publisher,
                check=True,
            )

            (repo / "tracked.txt").write_text("local\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "local conflict"], cwd=repo, check=True)
            local_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            local_tree = subprocess.check_output(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
            ).strip()

            (publisher / "tracked.txt").write_text("remote\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=publisher, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "remote conflict"],
                cwd=publisher,
                check=True,
            )
            subprocess.run(["git", "push", "-q", "origin", branch], cwd=publisher, check=True)

            with self.assertRaisesRegex(RepositoryBusyError, "merge conflicts"):
                _require_synchronized_remote_branch(repo, branch)

            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
                local_head,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
                ).strip(),
                local_tree,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "status", "--porcelain"], cwd=repo, text=True
                ),
                "",
            )

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_plain_release_command_fast_forwards_clean_behind_branch_and_preserves_clawpatch_state(
        self,
        _version,
        _processes,
        json_clawpatch,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            publisher = root / "publisher"
            remote = root / "remote.git"
            repo.mkdir()
            self.init_repo(repo)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True)

            subprocess.run(
                ["git", "clone", "-q", "--branch", branch, str(remote), str(publisher)],
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "tests@example.com"],
                cwd=publisher,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Tests"], cwd=publisher, check=True
            )
            (publisher / "published.txt").write_text("released\n", encoding="utf-8")
            subprocess.run(["git", "add", "published.txt"], cwd=publisher, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "published release"],
                cwd=publisher,
                check=True,
            )
            subprocess.run(["git", "push", "origin", branch], cwd=publisher, check=True)
            remote_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=publisher, text=True
            ).strip()

            clawpatch_state = repo / ".clawpatch" / "findings" / "fnd_existing.json"
            clawpatch_state.parent.mkdir(parents=True)
            clawpatch_state.write_text('{"findingId":"fnd_existing"}\n', encoding="utf-8")
            (repo / ".clawpatch" / "project.json").write_text(
                '{"name":"fixture"}\n', encoding="utf-8"
            )
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
                {"dryRun": True, "wouldReview": 1, "jobs": 1},
                {"reviewed": 1, "findings": 0},
                {"dryRun": True, "wouldReview": 0, "jobs": 1},
                {"finding": None, "status": "open", "next": "clawpatch report --status open"},
            ]
            final_closure.return_value = {"pushed": False}
            progress_events: list[dict[str, object]] = []

            release_sweep(
                repo,
                apply=True,
                branch="current",
                push_mode="each",
                integration_mode="external",
                progress=progress_events.append,
            )

            self.assertEqual(
                subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
                remote_head,
            )
            self.assertTrue((repo / "published.txt").is_file())
            self.assertEqual(
                clawpatch_state.read_text(encoding="utf-8"),
                '{"findingId":"fnd_existing"}\n',
            )
            self.assertTrue(
                any(event.get("phase") == "git-sync" for event in progress_events)
            )

    def test_exact_path_commit_and_push_verification_match_the_live_remote_sha(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            remote = root / "remote.git"
            repo.mkdir()
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "push", "-u", "origin", branch], cwd=repo, check=True)

            source.write_text("clawpatch repair\n", encoding="utf-8")
            state = repo / ".clawpatch" / "runs" / "state.json"
            state.parent.mkdir(parents=True)
            state.write_text("{}\n", encoding="utf-8")
            commit = _commit_attempt(repo, "fnd_one", ["app.py"], branch=branch)
            _push_and_verify(repo, branch, first=False)

            committed_paths = subprocess.check_output(
                ["git", "show", "--pretty=", "--name-only", commit], cwd=repo, text=True
            ).splitlines()
            local = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            remote_sha = subprocess.check_output(
                ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
                cwd=repo,
                text=True,
            ).split()[0]

        self.assertEqual(committed_paths, ["app.py"])
        self.assertEqual(remote_sha, local)

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_next_uses_structured_open_finding_and_validates_show_handoff(self, json_clawpatch):
        json_clawpatch.return_value = {
            "finding": {"id": "fnd_one", "status": "open"},
            "next": "clawpatch show --finding fnd_one",
        }
        finding_id, payload = _next_finding(Path("/repo"), env={})
        self.assertEqual(finding_id, "fnd_one")
        self.assertEqual(payload["finding"]["status"], "open")

        json_clawpatch.return_value = {
            "finding": {"id": "fnd_one", "status": "uncertain"},
            "next": "clawpatch show --finding fnd_one",
        }
        with self.assertRaisesRegex(SafetyError, "non-open"):
            _next_finding(Path("/repo"), env={})

        finding_id, _payload = _next_finding(Path("/repo"), env={}, status="uncertain")
        self.assertEqual(finding_id, "fnd_one")
        self.assertEqual(
            json_clawpatch.call_args.args[1],
            ["clawpatch", "next", "--status", "uncertain", "--json"],
        )

    def test_patch_attempt_comes_from_clawpatch_show_record(self):
        payload = {
            "patchAttempts": [
                {
                    "patchAttemptId": "pat_one",
                    "findingIds": ["fnd_one"],
                    "filesChanged": ["src/app.py"],
                }
            ]
        }
        record = _patch_attempt_from_show(payload, "pat_one", "fnd_one")
        self.assertEqual(record["filesChanged"], ["src/app.py"])

    @patch("clawpatch_supervise.clawpatch_release.shutil.which")
    def test_windows_resolves_clawpatch_command_shim_without_a_shell(self, which):
        which.return_value = r"C:\Users\Test\AppData\Roaming\npm\clawpatch.cmd"
        command = _platform_command(["clawpatch", "next", "--json"], platform_name="nt")
        self.assertEqual(command[0], which.return_value)
        self.assertEqual(command[1:], ["next", "--json"])

    @patch("clawpatch_supervise.clawpatch_release._run")
    @patch("clawpatch_supervise.clawpatch_release.shutil.which", return_value="powershell.exe")
    def test_windows_process_inventory_uses_native_powershell(self, _which, run):
        run.return_value = self.completed(
            ["powershell.exe"],
            json.dumps(
                {
                    "ProcessId": 42,
                    "CommandLine": "node C:/Users/Test/AppData/Roaming/npm/node_modules/clawpatch review",
                }
            ),
        )
        processes = _windows_clawpatch_processes(Path("C:/repo"))
        self.assertEqual(processes[0]["pid"], 42)
        self.assertIn("conservative", processes[0]["cwd"])
        self.assertEqual(run.call_args.args[0][0], "powershell.exe")

    def test_exhausted_checkpoint_follows_only_a_disjoint_supervisor_upgrade(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            finding_id = "fnd_one"
            finding_path = repo / ".clawpatch" / "findings" / f"{finding_id}.json"
            finding_path.parent.mkdir(parents=True)
            finding_path.write_text(
                json.dumps(
                    {
                        "findingId": finding_id,
                        "evidence": [{"path": "src/manageroo/release_ready.py"}],
                    }
                ),
                encoding="utf-8",
            )
            old_head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()
            (repo / "README.md").write_text("controller docs\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "controller upgrade"], cwd=repo, check=True
            )
            progress = {
                "finding_id": finding_id,
                "head_before": old_head,
                "phase": "stopped",
            }

            self.assertTrue(_checkpoint_can_follow_supervisor_upgrade(repo, progress))

            source = repo / "src" / "manageroo" / "release_ready.py"
            source.parent.mkdir(parents=True)
            source.write_text("changed finding source\n", encoding="utf-8")
            subprocess.run(["git", "add", str(source.relative_to(repo))], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "finding source changed"], cwd=repo, check=True
            )
            self.assertFalse(_checkpoint_can_follow_supervisor_upgrade(repo, progress))

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_head_advance_does_not_bless_ambiguous_legacy_checkpoint(
        self,
        _version,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            old_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            finding_path = repo / ".clawpatch" / "findings" / "fnd_one.json"
            finding_path.parent.mkdir(parents=True)
            finding_path.write_text(
                json.dumps(
                    {
                        "findingId": "fnd_one",
                        "evidence": [{"path": "app.py"}],
                    }
                ),
                encoding="utf-8",
            )
            source.write_text("interrupted repair\n", encoding="utf-8")
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=old_head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            checkpoint["version"] = 3
            checkpoint.pop("owned_source_fingerprint")
            progress_path = repo / ".manageroo/cache/clawpatch-release-progress.json"
            progress_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            source.write_text("manual operator edit\n", encoding="utf-8")
            source_before = source.read_bytes()
            (repo / "README.md").write_text("controller upgrade\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "controller upgrade"], cwd=repo, check=True
            )
            legacy = _load_release_progress(repo)
            self.assertTrue(_checkpoint_can_follow_supervisor_upgrade(repo, legacy))

            with self.assertRaisesRegex(
                SafetyError,
                "cannot prove exact checkpoint-owned source content",
            ):
                release_sweep(repo, apply=True, branch="current")

            preserved = _load_release_progress(repo)
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(preserved["head_before"], old_head)
            self.assertEqual(preserved["owned_source_fingerprint"], "")

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_one_command_preserves_changed_checkpoint_source_and_continues(
        self,
        _version,
        _processes,
        _gates,
        resume_stopped_attempt,
        json_clawpatch,
        show_finding,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            old_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            finding_path = repo / ".clawpatch" / "findings" / "fnd_one.json"
            finding_path.parent.mkdir(parents=True)
            finding_path.write_text(
                json.dumps(
                    {
                        "findingId": "fnd_one",
                        "evidence": [{"path": "app.py"}],
                    }
                ),
                encoding="utf-8",
            )
            source.write_text("checkpoint-owned repair\n", encoding="utf-8")
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=old_head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            self.assertTrue(checkpoint["owned_source_fingerprint"])

            source.write_text("later ambiguous bytes\n", encoding="utf-8")
            ambiguous_bytes = source.read_bytes()
            (repo / "README.md").write_text("controller upgrade\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "controller upgrade"],
                cwd=repo,
                check=True,
            )

            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [],
            }
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 1},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False, "needs_fresh_review": False}

            report = release_sweep(repo, apply=True, branch="current")

            recovery = report["ambiguous_checkpoint_recovery"]
            preserved_commit = recovery["preserved_commit"]
            preserved_bytes = subprocess.check_output(
                ["git", "show", f"{preserved_commit}:app.py"], cwd=repo
            )
            receipt = Path(recovery["receipt"])
            receipt_payload = json.loads(receipt.read_text(encoding="utf-8"))

            self.assertEqual(source.read_bytes(), b"before\n")
            self.assertEqual(preserved_bytes, ambiguous_bytes)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", recovery["preserved_ref"]], cwd=repo, text=True
                ).strip(),
                preserved_commit,
            )
            self.assertEqual(receipt_payload["preserved_commit"], preserved_commit)
            self.assertEqual(receipt_payload["paths"], ["app.py"])
            self.assertIsNone(_load_release_progress(repo))
            self.assertTrue(finding_path.is_file())

        resume_stopped_attempt.assert_not_called()
        show_finding.assert_called_once()

    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_non_supervisor_head_advance_does_not_bless_ambiguous_legacy_checkpoint(
        self,
        _version,
        _processes,
        resume_stopped_attempt,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            old_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("interrupted repair\n", encoding="utf-8")
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=old_head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            checkpoint["version"] = 3
            checkpoint.pop("owned_source_fingerprint")
            progress_path = repo / ".manageroo/cache/clawpatch-release-progress.json"
            progress_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            source.write_text("manual operator edit\n", encoding="utf-8")
            source_before = source.read_bytes()
            (repo / "user.txt").write_text("later committed work\n", encoding="utf-8")
            subprocess.run(["git", "add", "user.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "later user work"], cwd=repo, check=True)
            legacy = _load_release_progress(repo)
            self.assertFalse(_checkpoint_can_follow_supervisor_upgrade(repo, legacy))

            with self.assertRaisesRegex(
                SafetyError,
                "cannot prove exact checkpoint-owned source content",
            ):
                release_sweep(repo, apply=True, branch="current")

            preserved = _load_release_progress(repo)
            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(preserved["head_before"], old_head)
            self.assertEqual(preserved["owned_source_fingerprint"], "")
            resume_stopped_attempt.assert_not_called()

            extra = repo / "other.py"
            extra.write_text("another active edit\n", encoding="utf-8")
            with (
                patch(
                    "clawpatch_supervise.clawpatch_release._release_state_root",
                    return_value=repo / ".manageroo" / "cache",
                ),
                self.assertRaisesRegex(
                    RepositoryBusyError,
                    "waiting without discarding",
                ),
            ):
                release_sweep(
                    repo,
                    apply=True,
                    branch="current",
                    integration_mode="external",
                )

            self.assertEqual(source.read_bytes(), source_before)
            self.assertEqual(extra.read_text(encoding="utf-8"), "another active edit\n")

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_complete_review_uses_bounded_worker_waves_until_zero_pending(self, json_clawpatch):
        json_clawpatch.side_effect = [
            {"dryRun": True, "wouldReview": 12, "jobs": 4},
            {"run": "run-1", "reviewed": 4, "findings": 1, "jobs": 4},
            {"dryRun": True, "wouldReview": 8, "jobs": 4},
            {"run": "run-2", "reviewed": 4, "findings": 2, "jobs": 4},
            {"dryRun": True, "wouldReview": 4, "jobs": 4},
            {"run": "run-3", "reviewed": 4, "findings": 1, "jobs": 4},
            {"dryRun": True, "wouldReview": 0, "jobs": 4},
        ]
        result = _review_all_features(Path("/repo"), env={}, mapped_features=12)
        self.assertEqual(result["review"]["reviewed"], 12)
        self.assertEqual(result["review"]["findings"], 4)
        self.assertEqual(result["review"]["runs"], ["run-1", "run-2", "run-3"])
        self.assertEqual(
            json_clawpatch.call_args_list[0].args[1],
            ["clawpatch", "review", "--limit", "12", "--dry-run", "--json"],
        )
        self.assertEqual(
            json_clawpatch.call_args_list[1].args[1],
            ["clawpatch", "review", "--limit", "4", "--json"],
        )
        self.assertEqual(
            json_clawpatch.call_args_list[3].args[1],
            ["clawpatch", "review", "--limit", "4", "--json"],
        )
        self.assertEqual(
            json_clawpatch.call_args_list[5].args[1],
            ["clawpatch", "review", "--limit", "4", "--json"],
        )

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_complete_review_stops_when_a_batch_does_not_reduce_pending_features(
        self, json_clawpatch
    ):
        json_clawpatch.side_effect = [
            {"dryRun": True, "wouldReview": 12, "jobs": 4},
            {"run": "run-1", "reviewed": 4, "findings": 1, "jobs": 4},
            {"dryRun": True, "wouldReview": 12, "jobs": 4},
        ]

        with self.assertRaisesRegex(SafetyError, "did not reduce pending features"):
            _review_all_features(Path("/repo"), env={}, mapped_features=12)

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._run")
    def test_fix_exit_six_marks_attempt_unresolved(self, run, _processes):
        run.return_value = self.completed(
            ["clawpatch", "fix"],
            "error: validation failed after applying fix\n",
            6,
        )

        with self.assertRaisesRegex(SafetyError, "exit code: 6") as raised:
            _fix_command(Path("/repo"), ["clawpatch", "fix", "--finding", "fnd_one"])

        self.assertEqual(raised.exception.outcome, "fix-validation-failed")

        self.assertEqual(
            run.call_args.args[0],
            ["clawpatch", "fix", "--finding", "fnd_one", "--json"],
        )
        self.assertTrue(run.call_args.kwargs["kill_process_group"])
        self.assertEqual(run.call_args.kwargs["timeout"], 900)

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._run")
    def test_fix_timeout_is_not_retried_and_kills_the_complete_child_group(self, run, _processes):
        run.return_value = self.completed(
            ["clawpatch", "fix"],
            "partial child output\nTIMEOUT\n",
            124,
        )

        with self.assertRaisesRegex(SafetyError, "exit code: 124") as raised:
            _fix_command(Path("/repo"), ["clawpatch", "fix", "--finding", "fnd_one"])

        self.assertEqual(raised.exception.outcome, "timeout")
        self.assertTrue(run.call_args.kwargs["kill_process_group"])
        self.assertEqual(run.call_args.kwargs["timeout"], 900)

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_uncertain_read_only_revalidation_escalates_without_rerunning_fix(self, json_clawpatch):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("clawpatch repair\n", encoding="utf-8")
            json_clawpatch.side_effect = [
                {
                    "finding": "fnd_one",
                    "outcome": "uncertain",
                    "reason": "targeted tests could not create a temporary directory",
                },
                {"finding": "fnd_one", "outcome": "fixed"},
            ]
            env = {"PATH": "/bin"}

            result = _revalidate(
                repo,
                "fnd_one",
                env=env,
                expected_paths=["app.py"],
            )

        self.assertEqual(result["outcome"], "fixed")
        self.assertTrue(result["managerooSandboxEscalated"])
        self.assertEqual(result["managerooInitialOutcome"], "uncertain")
        self.assertEqual(json_clawpatch.call_count, 2)
        self.assertNotIn("CLAWPATCH_CODEX_SANDBOX", env)
        self.assertNotIn(
            "CLAWPATCH_CODEX_SANDBOX",
            json_clawpatch.call_args_list[0].kwargs["env"],
        )
        self.assertEqual(
            json_clawpatch.call_args_list[1].kwargs["env"]["CLAWPATCH_CODEX_SANDBOX"],
            "workspace-write",
        )

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_external_uncertain_revalidation_uses_trusted_host_after_workspace_block(
        self, json_clawpatch
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("clawpatch repair\n", encoding="utf-8")
            json_clawpatch.side_effect = [
                {"finding": "fnd_one", "outcome": "uncertain"},
                {
                    "finding": "fnd_one",
                    "outcome": "uncertain",
                    "reasoning": "Gradle socket-based lock service was sandbox-blocked",
                },
                {"finding": "fnd_one", "outcome": "fixed"},
            ]
            env = {"MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK": "1"}

            result = _revalidate(
                repo,
                "fnd_one",
                env=env,
                expected_paths=["app.py"],
            )

        self.assertEqual(result["outcome"], "fixed")
        self.assertTrue(result["managerooHostSandboxBypassed"])
        self.assertEqual(result["managerooWorkspaceWriteOutcome"], "uncertain")
        self.assertEqual(json_clawpatch.call_count, 3)
        self.assertEqual(
            json_clawpatch.call_args_list[1].kwargs["env"]["CLAWPATCH_CODEX_SANDBOX"],
            "workspace-write",
        )
        self.assertEqual(
            json_clawpatch.call_args_list[2].kwargs["env"]["CLAWPATCH_CODEX_SANDBOX"],
            "bypass",
        )

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_uncertain_after_full_revalidation_ladder_reaches_transition_policy(
        self, json_clawpatch
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("clawpatch repair\n", encoding="utf-8")
            json_clawpatch.side_effect = [
                {"finding": "fnd_one", "outcome": "uncertain"},
                {"finding": "fnd_one", "outcome": "uncertain"},
                {
                    "finding": "fnd_one",
                    "outcome": "uncertain",
                    "reasoning": "repair works but stale assertions still fail",
                },
            ]

            result = _revalidate(
                repo,
                "fnd_one",
                env={"MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK": "1"},
                expected_paths=["app.py"],
            )

        self.assertEqual(result["outcome"], "uncertain")
        self.assertTrue(result["managerooHostSandboxBypassed"])
        self.assertEqual(result["managerooWorkspaceWriteOutcome"], "uncertain")

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_external_open_revalidation_uses_trusted_host_after_sandbox_block(self, json_clawpatch):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("repair already committed\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "repair"], cwd=repo, check=True)
            json_clawpatch.side_effect = [
                {
                    "finding": "fnd_one",
                    "outcome": "open",
                    "reasoning": "validator could not bind 127.0.0.1 in the read-only sandbox",
                },
                {
                    "finding": "fnd_one",
                    "outcome": "open",
                    "reasoning": "validator could not bind 127.0.0.1 in workspace-write",
                },
                {"finding": "fnd_one", "outcome": "fixed"},
            ]
            env = {"MANAGEROO_CLAWPATCH_ALLOW_BYPASS_FALLBACK": "1"}

            result = _revalidate(
                repo,
                "fnd_one",
                env=env,
                expected_paths=[],
            )

        self.assertEqual(result["outcome"], "fixed")
        self.assertTrue(result["managerooSandboxEscalated"])
        self.assertTrue(result["managerooHostSandboxBypassed"])
        self.assertEqual(result["managerooInitialOutcome"], "open")
        self.assertEqual(result["managerooWorkspaceWriteOutcome"], "open")
        self.assertEqual(json_clawpatch.call_count, 3)
        self.assertEqual(
            json_clawpatch.call_args_list[1].kwargs["env"]["CLAWPATCH_CODEX_SANDBOX"],
            "workspace-write",
        )
        self.assertEqual(
            json_clawpatch.call_args_list[2].kwargs["env"]["CLAWPATCH_CODEX_SANDBOX"],
            "bypass",
        )

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_open_revalidation_returns_the_documented_same_finding_continuation(
        self, json_clawpatch
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("partial clawpatch repair\n", encoding="utf-8")
            json_clawpatch.return_value = {
                "finding": "fnd_one",
                "outcome": "open",
                "reasoning": "the same finding still needs another Clawpatch fix",
            }

            result = _revalidate(
                repo,
                "fnd_one",
                env={},
                expected_paths=["app.py"],
            )

        self.assertEqual(result["outcome"], "open")
        self.assertTrue(result["managerooSandboxEscalated"])
        self.assertEqual(result["managerooInitialOutcome"], "open")
        self.assertEqual(json_clawpatch.call_count, 2)
        self.assertEqual(
            json_clawpatch.call_args_list[1].kwargs["env"]["CLAWPATCH_CODEX_SANDBOX"],
            "workspace-write",
        )

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._commit_attempt", return_value="partial123")
    @patch(
        "clawpatch_supervise.clawpatch_release._revalidate",
        return_value={"finding": "fnd_one", "outcome": "open"},
    )
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._validate_attempt_paths")
    @patch(
        "clawpatch_supervise.clawpatch_release._patch_attempt_from_show",
        return_value={"filesChanged": ["app.py"]},
    )
    @patch(
        "clawpatch_supervise.clawpatch_release._show_finding",
        return_value={"finding": {"id": "fnd_one", "status": "uncertain"}},
    )
    @patch(
        "clawpatch_supervise.clawpatch_release._fix_command",
        return_value={"patchAttempt": "pat_one"},
    )
    @patch("clawpatch_supervise.clawpatch_release._require_no_process")
    @patch("clawpatch_supervise.clawpatch_release._source_paths", return_value=[])
    def test_execute_fix_leaves_open_attempt_for_local_iteration_controller(
        self,
        _source_paths,
        _no_process,
        _fix,
        _show,
        _patch_attempt,
        _validate_paths,
        _gates,
        _revalidation,
        commit_attempt,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            record, pushed = _execute_fix(
                repo,
                "fnd_one",
                inspected={"finding": {"id": "fnd_one", "status": "open"}},
                env={},
                push_mode="each",
                branch=branch,
                pushed=False,
                require_project_gates=False,
            )

        self.assertEqual(record["revalidation"]["outcome"], "open")
        self.assertEqual(record["commit"], "")
        self.assertFalse(pushed)
        commit_attempt.assert_not_called()
        push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch(
        "clawpatch_supervise.clawpatch_release._revalidate",
        return_value={"finding": "fnd_one", "outcome": "open"},
    )
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_open_attempt_remains_local_for_same_finding_iteration(
        self,
        show_finding,
        _gates,
        revalidate,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("partial Clawpatch repair\n", encoding="utf-8")
            checkpoint = {
                "finding_id": "fnd_one",
                "branch": branch,
                "head_before": head,
                "phase": "stopped",
                "owned_paths": ["app.py"],
            }
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="each",
                branch=branch,
                pushed=False,
                require_project_gates=False,
            )

            status = subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=repo, text=True
            )

        self.assertTrue(record["resumed"])
        self.assertEqual(record["patch_attempt"], "pat_one")
        self.assertEqual(record["revalidation"]["outcome"], "open")
        self.assertEqual(record["commit"], "")
        self.assertIn("app.py", status)
        self.assertFalse(pushed)
        revalidate.assert_called_once()
        push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_open_gate_failure_reenters_same_finding_with_exact_evidence(
        self,
        show_finding,
        run_project_gates,
        revalidate,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("checkpointed repair\n", encoding="utf-8")
            checkpoint = {
                "finding_id": "fnd_one",
                "branch": branch,
                "head_before": head,
                "phase": "stopped",
                "owned_paths": ["app.py"],
            }
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }
            run_project_gates.side_effect = GateFailure(
                "gate: manageroo-release\n"
                "failed requirement: complete repository validation must pass\n"
                "release-validation-is-complete: false"
            )

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
            )

            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)

        self.assertFalse(pushed)
        self.assertEqual(record["revalidation"]["outcome"], "open")
        self.assertTrue(
            record["revalidation"]["managerooProjectGateFailureContinuation"]
        )
        self.assertIn(
            "release-validation-is-complete: false",
            record["revalidation"]["managerooProjectGateFailure"],
        )
        self.assertEqual(record["gate_runs"], [])
        self.assertIn("app.py", status)
        revalidate.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_uncertain_gate_failure_does_not_commit_or_advance(
        self,
        show_finding,
        run_project_gates,
        revalidate,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("checkpointed repair\n", encoding="utf-8")
            checkpoint = {
                "finding_id": "fnd_one",
                "branch": branch,
                "head_before": head,
                "phase": "stopped",
                "owned_paths": ["app.py"],
            }
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }
            run_project_gates.side_effect = GateFailure("configured project gate failed")

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="each",
                branch=branch,
                pushed=False,
                advance_uncertain=True,
            )

            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)

        self.assertFalse(pushed)
        self.assertEqual(record["revalidation"]["outcome"], "uncertain")
        self.assertNotIn("deferred_uncertain", record)
        self.assertEqual(record["commit"], "")
        self.assertIn("app.py", status)
        revalidate.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_open_invalid_gate_configuration_stays_stopped(
        self,
        show_finding,
        run_project_gates,
        revalidate,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("checkpointed repair\n", encoding="utf-8")
            checkpoint = {
                "finding_id": "fnd_one",
                "branch": branch,
                "head_before": head,
                "phase": "stopped",
                "owned_paths": ["app.py"],
            }
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }
            run_project_gates.side_effect = SafetyError(
                "Validation gate manageroo-release uses unapproved executable 'python3'."
            )

            with self.assertRaisesRegex(SafetyError, "unapproved executable"):
                _resume_stopped_attempt(
                    repo,
                    checkpoint,
                    env={},
                    push_mode="none",
                    branch=branch,
                    pushed=False,
                )

        revalidate.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_fixed_attempt_ignores_proven_untracked_node_modules_noise(
        self,
        show_finding,
        _gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            scripts = repo / "scripts"
            scripts.mkdir()
            package = repo / "package.json"
            check = scripts / "check.mjs"
            package.write_text('{"scripts": {"check": "node scripts/check.mjs"}}\n')
            check.write_text("// assertions\n", encoding="utf-8")
            subprocess.run(["git", "add", "package.json", "scripts/check.mjs"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            package.write_text(
                '{"scripts": {"check": "node scripts/check.mjs", '
                '"test": "node scripts/check.mjs"}}\n',
                encoding="utf-8",
            )
            check.write_text("// assertions\n// test script assertion\n", encoding="utf-8")
            dependency = repo / "node_modules" / "entities"
            dependency.mkdir(parents=True)
            (dependency / "LICENSE").write_text("dependency license\n", encoding="utf-8")
            old_owned_paths = [
                "node_modules/entities/LICENSE",
                "package.json",
                "scripts/check.mjs",
            ]
            checkpoint = {
                "finding_id": "fnd_one",
                "branch": "main",
                "head_before": head,
                "phase": "stopped",
                "owned_paths": old_owned_paths,
                "owned_source_fingerprint": _source_paths_fingerprint(
                    repo, old_owned_paths
                ),
            }
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "fixed"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["package.json", "scripts/check.mjs"],
                        "git": {"baseSha": head},
                    }
                ],
            }

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="none",
                branch="main",
                pushed=False,
                require_project_gates=False,
            )

            committed_paths = subprocess.check_output(
                ["git", "show", "--pretty=format:", "--name-only", "HEAD"],
                cwd=repo,
                text=True,
            ).split()
            remaining_source = _source_paths(repo)
            dependency_exists = (repo / "node_modules" / "entities" / "LICENSE").is_file()

        self.assertFalse(pushed)
        self.assertEqual(record["revalidation"]["outcome"], "fixed")
        self.assertEqual(record["files_changed"], ["package.json", "scripts/check.mjs"])
        self.assertEqual(committed_paths, ["package.json", "scripts/check.mjs"])
        self.assertEqual(remaining_source, [])
        self.assertTrue(dependency_exists)

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_revalidation_mutation_reenters_open_finding(
        self,
        show_finding,
        _gates,
        revalidate,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("partial Clawpatch repair\n", encoding="utf-8")
            checkpoint = {
                "finding_id": "fnd_one",
                "branch": branch,
                "head_before": head,
                "phase": "stopped",
                "owned_paths": ["app.py"],
            }
            uncertain = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }
            reopened = {
                **uncertain,
                "finding": {"id": "fnd_one", "status": "open"},
            }
            show_finding.side_effect = [uncertain, reopened]
            revalidate.side_effect = _UnresolvedFinding(
                "revalidation changed source",
                finding_id="fnd_one",
                outcome="revalidation-mutated-source",
            )

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                require_project_gates=False,
            )

        self.assertEqual(record["revalidation"]["outcome"], "open")
        self.assertTrue(record["revalidation"]["managerooRevalidationProgress"])
        self.assertFalse(pushed)
        self.assertEqual(show_finding.call_count, 2)
        push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_revalidation_provider_failure_reenters_same_finding_fix(
        self,
        show_finding,
        _gates,
        revalidate,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("checkpointed repair\n", encoding="utf-8")
            checkpoint = {
                "finding_id": "fnd_one",
                "branch": branch,
                "head_before": head,
                "phase": "stopped",
                "owned_paths": ["app.py"],
            }
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }
            revalidate.side_effect = _UnresolvedFinding(
                "Codex revalidation provider failed",
                finding_id="fnd_one",
                outcome="revalidation-provider-failed",
            )

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                require_project_gates=False,
            )

        self.assertEqual(record["revalidation"]["outcome"], "open")
        self.assertTrue(record["revalidation"]["managerooProviderFailureContinuation"])
        self.assertFalse(pushed)
        push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_multi_attempt_chain_resumes_from_checkpoint_owned_combined_repair(
        self,
        show_finding,
        _gates,
        revalidate,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            test = repo / "test_app.py"
            source.write_text("before\n", encoding="utf-8")
            test.write_text("before test\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "test_app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("combined partial repair\n", encoding="utf-8")
            test.write_text("combined regression test\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "test_app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    "manageroo clawpatch iteration: fnd_one",
                ],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source_state = subprocess.check_output(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=["app.py", "test_app.py"],
                temporary_commit=temporary_commit,
                source_states=[source_state],
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_partial",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py", "test_app.py"],
                        "git": {"baseSha": original_head},
                    },
                    {
                        "patchAttemptId": "pat_no_edit",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": temporary_commit},
                    },
                ],
            }

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="each",
                branch=branch,
                pushed=False,
                require_project_gates=False,
            )

            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)

        self.assertTrue(record["resumed"])
        self.assertEqual(record["patch_attempt"], "pat_no_edit")
        self.assertEqual(record["revalidation"]["outcome"], "open")
        self.assertIn("app.py", status)
        self.assertIn("test_app.py", status)
        self.assertFalse(pushed)
        revalidate.assert_not_called()
        push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_chain_accepts_verified_tree_identical_prior_iteration_boundary(
        self,
        show_finding,
        _gates,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            source.write_text("checkpoint-owned repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "manageroo clawpatch iteration: fnd_one"],
                cwd=repo,
                check=True,
            )
            prior_temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    "clawpatch-supervise iteration: fnd_one",
                ],
                cwd=repo,
                check=True,
            )
            checkpoint_temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            self.assertNotEqual(prior_temporary_commit, checkpoint_temporary_commit)
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-parse", f"{prior_temporary_commit}^{{tree}}"],
                    cwd=repo,
                    text=True,
                ).strip(),
                subprocess.check_output(
                    ["git", "rev-parse", f"{checkpoint_temporary_commit}^{{tree}}"],
                    cwd=repo,
                    text=True,
                ).strip(),
            )
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            source.write_text("different repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "clawpatch fix: fnd_one"],
                cwd=repo,
                check=True,
            )
            mismatched_temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            source.write_text("checkpoint-owned repair\n", encoding="utf-8")
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=["app.py"],
                temporary_commit=checkpoint_temporary_commit,
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_no_edit",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": prior_temporary_commit},
                    },
                    {
                        "patchAttemptId": "pat_different_tree",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": mismatched_temporary_commit},
                    },
                    {
                        "patchAttemptId": "pat_malformed_boundary",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": ["not", "a", "sha"]},
                    },
                ],
            }

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                require_project_gates=False,
            )

        self.assertTrue(record["resumed"])
        self.assertEqual(record["patch_attempt"], "pat_no_edit")
        self.assertFalse(pushed)
        push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._push_and_verify")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_revalidation_progress_can_extend_temporary_commit_paths(
        self,
        show_finding,
        _gates,
        push_and_verify,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            validation = repo / "BUILD-VALIDATION.json"
            source.write_text("before\n", encoding="utf-8")
            validation.write_text('{"proof": "before"}\n', encoding="utf-8")
            subprocess.run(
                ["git", "add", "app.py", "BUILD-VALIDATION.json"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("first partial repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "manageroo clawpatch iteration: fnd_one"],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            source.write_text("second partial repair\n", encoding="utf-8")
            validation.write_text('{"proof": "revalidated"}\n', encoding="utf-8")
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=["BUILD-VALIDATION.json", "app.py"],
                temporary_commit=temporary_commit,
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_second",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": temporary_commit},
                    }
                ],
            }

            record, pushed = _resume_stopped_attempt(
                repo,
                checkpoint,
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                require_project_gates=False,
            )

        self.assertTrue(record["resumed"])
        self.assertEqual(record["files_changed"], ["BUILD-VALIDATION.json", "app.py"])
        self.assertEqual(record["revalidation"]["outcome"], "open")
        self.assertFalse(pushed)
        push_and_verify.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    def test_stopped_multi_attempt_chain_rejects_changed_source_fingerprint(
        self,
        show_finding,
        run_project_gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("checkpoint-owned repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    "manageroo clawpatch iteration: fnd_one",
                ],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=["app.py"],
                temporary_commit=temporary_commit,
            )
            source.write_text("unowned later change\n", encoding="utf-8")
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_failed",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": temporary_commit},
                    }
                ],
            }

            with self.assertRaisesRegex(SafetyError, "exact source fingerprint"):
                _resume_stopped_attempt(
                    repo,
                    checkpoint,
                    env={},
                    push_mode="each",
                    branch=branch,
                    pushed=False,
                    require_project_gates=False,
                )

        run_project_gates.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_relaunch_provider_refusal_reenters_same_uncertain_finding_fix(
        self,
        _version,
        _processes,
        show_finding,
        _gates,
        revalidate,
        json_clawpatch,
        review_all,
        next_finding,
        execute_fix,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("first partial Clawpatch repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "clawpatch continuation: fnd_one"],
                cwd=repo,
                check=True,
            )
            attempt_base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("second partial Clawpatch repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            inspection = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": attempt_base},
                    }
                ],
            }
            show_finding.side_effect = [
                inspection,
                {
                    "finding": {"id": "fnd_one", "status": "uncertain"},
                    "validation": [],
                    "patchAttempts": [inspection["patchAttempts"][0]],
                },
            ]
            revalidate.side_effect = _UnresolvedFinding(
                "Codex revalidation provider failed",
                finding_id="fnd_one",
                outcome="revalidation-provider-failed",
            )
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 1,
            }
            next_finding.side_effect = [(None, {"finding": None})]

            def complete_fix(*_args, **_kwargs):
                source.write_text("completed Clawpatch repair\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = complete_fix
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")

        self.assertEqual(len(report["continuations"]), 1)
        self.assertTrue(report["continuations"][0]["resumed"])
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(execute_fix.call_count, 1)
        self.assertIsNone(show_finding.call_args_list[1].kwargs["required_status"])
        self.assertEqual(next_finding.call_count, 1)
        review_all.assert_not_called()
        self.assertEqual(
            [invocation.args[1] for invocation in json_clawpatch.call_args_list],
            [["clawpatch", "status", "--json"]],
        )
        self.assertIsNone(_load_release_progress(repo))

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_relaunch_uses_uncertain_validation_evidence_for_same_finding_fix(
        self,
        _version,
        _processes,
        show_finding,
        _gates,
        revalidate,
        json_clawpatch,
        review_all,
        next_finding,
        execute_fix,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("checkpointed partial repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
                last_action=RepairAction.STOP_TERMINAL,
            )
            inspection = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_one",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }
            show_finding.side_effect = [inspection, inspection]
            revalidate.return_value = {
                "finding": "fnd_one",
                "outcome": "uncertain",
                "reasoning": "the repair works but stale assertions still need correction",
            }
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 1,
            }
            next_finding.return_value = (None, {"finding": None})

            def complete_fix(*_args, **_kwargs):
                source.write_text("completed repair and corrected assertions\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = complete_fix
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")

        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(execute_fix.call_count, 1)
        self.assertEqual(show_finding.call_args_list[1].kwargs["required_status"], None)
        self.assertEqual(len(report["continuations"]), 1)
        self.assertTrue(report["continuations"][0]["resumed"])
        review_all.assert_not_called()
        self.assertIsNone(_load_release_progress(repo))

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_relaunch_recovers_interrupted_fix_phase_without_source_changes(
        self,
        _version,
        _processes,
        show_finding,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        execute_fix,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source_tree = subprocess.check_output(
                ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, text=True
            ).strip()
            clawpatch_state = repo / ".clawpatch"
            clawpatch_state.mkdir()
            (clawpatch_state / "project.json").write_text("{}\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="fix",
                owned_paths=[],
                source_states=[source_tree],
            )
            (repo / "supervisor-upgrade.py").write_text("upgrade\n", encoding="utf-8")
            subprocess.run(["git", "add", "supervisor-upgrade.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "supervisor upgrade"],
                cwd=repo,
                check=True,
            )
            interrupted = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_interrupted",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": head},
                    }
                ],
            }
            show_finding.side_effect = [interrupted, interrupted]
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 1,
            }
            queue = {
                "finding": {"id": "fnd_one", "status": "open"},
                "next": "clawpatch show --finding fnd_one",
            }
            next_finding.side_effect = [("fnd_one", queue), (None, {"finding": None})]

            def complete_fix(*_args, **_kwargs):
                (repo / "fixed.py").write_text("fixed\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["fixed.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = complete_fix
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")

        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(execute_fix.call_count, 1)
        self.assertEqual(next_finding.call_count, 2)
        resume_stopped.assert_not_called()
        review_all.assert_not_called()
        self.assertEqual(
            report["interrupted_phase_recovery"]["prior_phase"],
            "fix",
        )
        self.assertIsNone(_load_release_progress(repo))

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_relaunch_consumes_exact_fixed_zero_source_checkpoint_and_advances_queue(
        self,
        _version,
        _processes,
        show_finding,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        execute_fix,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            clawpatch_state = repo / ".clawpatch"
            clawpatch_state.mkdir()
            (clawpatch_state / "project.json").write_text("{}\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_overlap",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=[],
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_overlap", "status": "fixed"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_already_fixed",
                        "status": "applied",
                        "findingIds": ["fnd_overlap"],
                        "filesChanged": [],
                        "git": {"baseSha": head},
                    }
                ],
            }
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 0,
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            final_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)

        self.assertEqual(final_head, head)
        self.assertEqual(status, "")
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(report["results"][0]["finding_id"], "fnd_overlap")
        self.assertEqual(report["results"][0]["patch_attempt"], "pat_already_fixed")
        self.assertEqual(report["results"][0]["files_changed"], [])
        self.assertEqual(report["results"][0]["commit"], "")
        self.assertEqual(report["results"][0]["revalidation"]["outcome"], "fixed")
        self.assertEqual(next_finding.call_count, 1)
        execute_fix.assert_not_called()
        resume_stopped.assert_not_called()
        review_all.assert_not_called()
        self.assertIsNone(_load_release_progress(repo))

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_resume_consumes_false_positive_checkpoint_that_returned_to_original_tree(
        self,
        _version,
        _processes,
        show_finding,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        execute_fix,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("correct original\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("incorrect attempted repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    "clawpatch-supervise iteration: fnd_false",
                ],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)
            source.write_text("correct original\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_false",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=[],
                temporary_commit=temporary_commit,
                last_action=RepairAction.STOP_TERMINAL,
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_false", "status": "false-positive"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_false",
                        "status": "applied",
                        "findingIds": ["fnd_false"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": original_head},
                    }
                ],
            }
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 0,
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            final_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)

        self.assertEqual(final_head, original_head)
        self.assertEqual(status, "")
        self.assertEqual(report["finding_count"], 0)
        self.assertEqual(report["false_positive_count"], 1)
        self.assertEqual(report["false_positives"][0]["finding_id"], "fnd_false")
        self.assertEqual(report["false_positives"][0]["temporary_commit"], temporary_commit)
        self.assertEqual(next_finding.call_count, 1)
        execute_fix.assert_not_called()
        resume_stopped.assert_not_called()
        review_all.assert_not_called()
        self.assertIsNone(_load_release_progress(repo))

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_resume_discards_exact_owned_false_positive_source_and_advances(
        self,
        _version,
        _processes,
        show_finding,
        _gates,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("correct original\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("incorrect attempted repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_false",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
                last_action=RepairAction.DISCARD_AND_CONTINUE,
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_false", "status": "false-positive"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_false",
                        "status": "applied",
                        "findingIds": ["fnd_false"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                    }
                ],
            }
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 0,
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
            restored = source.read_text(encoding="utf-8")

        self.assertEqual(restored, "correct original\n")
        self.assertEqual(status, "")
        self.assertEqual(report["false_positive_count"], 1)
        self.assertEqual(report["false_positives"][0]["finding_id"], "fnd_false")
        self.assertEqual(report["false_positives"][0]["discarded_paths"], ["app.py"])
        self.assertEqual(next_finding.call_count, 1)
        review_all.assert_not_called()
        self.assertIsNone(_load_release_progress(repo))

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._process_finding_until_fixed")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_empty_checkpoint_without_patch_attempt_retries_the_same_open_finding(
        self,
        _version,
        _processes,
        show_finding,
        json_clawpatch,
        next_finding,
        process_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            (repo / ".clawpatch").mkdir()
            (repo / ".clawpatch" / "project.json").write_text("{}\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=[],
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [],
            }
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 1,
            }
            next_finding.side_effect = [
                ("fnd_one", {"finding": {"id": "fnd_one"}}),
                (None, {"finding": None}),
            ]
            process_finding.return_value = (
                {"finding_id": "fnd_one", "files_changed": [], "commit": "abc123"},
                False,
                0,
            )
            final_closure.return_value = {"pushed": False, "needs_fresh_review": False}

            report = release_sweep(repo, apply=True, branch="current")
            checkpoint = _load_release_progress(repo)

        self.assertIsNone(checkpoint)
        self.assertEqual(report["interrupted_unapplied_attempt"]["finding_id"], "fnd_one")
        self.assertEqual(report["interrupted_unapplied_attempt"]["patch_attempts"], [])
        process_finding.assert_called_once()

    def test_empty_checkpoint_recognizes_latest_applied_attempt_at_same_head(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            regression = repo / "test_app.py"
            source.write_text("before\n", encoding="utf-8")
            regression.write_text("before test\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "test_app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=[],
            )
            source.write_text("correct repair\n", encoding="utf-8")
            regression.write_text("correct regression\n", encoding="utf-8")
            inspected = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_no_edit",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": head},
                    },
                    {
                        "patchAttemptId": "pat_repair",
                        "status": "applied",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py", "test_app.py"],
                        "git": {"baseSha": head},
                    },
                ],
            }

            recovered = _checkpoint_later_applied_attempt(
                repo,
                checkpoint,
                inspected=inspected,
            )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["patch_attempt"]["patchAttemptId"], "pat_repair")
        self.assertEqual(recovered["owned_paths"], ["app.py", "test_app.py"])

    def test_stale_checkpoint_recognizes_later_validation_failed_attempt_at_current_head(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            old_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            source.write_text("first interrupted repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "clawpatch-supervise iteration: fnd_one"],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", old_head], cwd=repo, check=True)
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch="main",
                head_before=old_head,
                phase="stopped",
                owned_paths=["app.py"],
                temporary_commit=temporary_commit,
                last_action=RepairAction.STOP_TERMINAL,
            )

            (repo / "supervisor.py").write_text("upgrade\n", encoding="utf-8")
            subprocess.run(["git", "add", "supervisor.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "supervisor progress"], cwd=repo, check=True
            )
            current_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("later validation-failed repair\n", encoding="utf-8")
            attempt_base = current_head
            (repo / "release.py").write_text("later supervisor release\n", encoding="utf-8")
            subprocess.run(["git", "add", "release.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "release supervisor"], cwd=repo, check=True)
            inspected = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_later_failed",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": attempt_base},
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "updatedAt": "2099-01-01T00:01:00.000Z",
                    }
                ],
            }

            recovered = _checkpoint_later_applied_attempt(
                repo,
                checkpoint,
                inspected=inspected,
            )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["patch_attempt"]["patchAttemptId"], "pat_later_failed")
        self.assertEqual(recovered["owned_paths"], ["app.py"])

    def test_stale_checkpoint_does_not_claim_source_edited_after_failed_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            checkpoint = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch="main",
                head_before=head,
                phase="stopped",
                owned_paths=[],
            )
            source.write_text("manual edit after failed attempt\n", encoding="utf-8")
            os.utime(source, (4_133_980_800, 4_133_980_800))
            inspected = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_failed",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": head},
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "updatedAt": "2099-01-01T00:01:00.000Z",
                    }
                ],
            }

            recovered = _checkpoint_later_applied_attempt(
                repo,
                checkpoint,
                inspected=inspected,
            )

        self.assertIsNone(recovered)

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_relaunch_rebinds_stale_checkpoint_to_later_validation_failed_attempt(
        self,
        _version,
        _processes,
        show_finding,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            old_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("first interrupted repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "clawpatch-supervise iteration: fnd_one"],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", old_head], cwd=repo, check=True)
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch="main",
                head_before=old_head,
                phase="stopped",
                owned_paths=["app.py"],
                temporary_commit=temporary_commit,
                last_action=RepairAction.STOP_TERMINAL,
            )
            (repo / "supervisor.py").write_text("upgrade\n", encoding="utf-8")
            subprocess.run(["git", "add", "supervisor.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "supervisor progress"], cwd=repo, check=True
            )
            current_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("later validation-failed repair\n", encoding="utf-8")
            inspection = {
                "finding": {"id": "fnd_one", "status": "uncertain"},
                "validation": [],
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_later_failed",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": ["app.py"],
                        "git": {"baseSha": current_head},
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "updatedAt": "2099-01-01T00:01:00.000Z",
                    }
                ],
            }
            show_finding.return_value = inspection

            def finish_resumed_attempt(*_args, **_kwargs):
                subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
                subprocess.run(
                    ["git", "commit", "-q", "-m", "finish resumed repair"],
                    cwd=repo,
                    check=True,
                )
                return (
                    {
                        "finding_id": "fnd_one",
                        "inspection": inspection,
                        "head_before": current_head,
                        "patch_attempt": "pat_later_failed",
                        "files_changed": ["app.py"],
                        "gate_runs": [],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                        ).strip(),
                    },
                    False,
                )

            resume_stopped.side_effect = finish_resumed_attempt
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 0,
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            rebound = resume_stopped.call_args.args[1]

        self.assertEqual(rebound["head_before"], current_head)
        self.assertEqual(rebound["owned_paths"], ["app.py"])
        self.assertEqual(rebound["temporary_commit"], "")
        self.assertEqual(rebound["last_action"], RepairAction.STOP_TRANSIENT.value)
        self.assertEqual(
            report["recovered_later_applied_attempt"]["patch_attempt"],
            "pat_later_failed",
        )
        review_all.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_fixed_zero_source_checkpoint_without_applied_attempt_stays_stopped(
        self,
        _version,
        _processes,
        show_finding,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            (repo / ".clawpatch").mkdir()
            (repo / ".clawpatch" / "project.json").write_text("{}\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=[],
            )
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "fixed"},
                "validation": [],
                "patchAttempts": [],
            }

            with self.assertRaisesRegex(SafetyError, "applied zero-file patch attempt"):
                release_sweep(repo, apply=True, branch="current")
            checkpoint = _load_release_progress(repo)

        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["head_before"], head)

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_rebuilt_clawpatch_generation_discards_only_fingerprinted_interrupted_source(
        self,
        _version,
        _processes,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            test = repo / "test_app.py"
            source.write_text("before\n", encoding="utf-8")
            test.write_text("before test\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "test_app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("interrupted repair\n", encoding="utf-8")
            test.write_text("interrupted regression test\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_old",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py", "test_app.py"],
            )
            state = repo / ".clawpatch"
            (state / "features").mkdir(parents=True)
            (state / "findings").mkdir()
            (state / "patches").mkdir()
            (state / "runs").mkdir()
            (state / "reports").mkdir()
            (state / "project.json").write_text(
                json.dumps(
                    {
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "git": {"headSha": head, "currentBranch": branch},
                    }
                ),
                encoding="utf-8",
            )
            (state / "features" / "feat_new.json").write_text("{}\n", encoding="utf-8")
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 0},
                "completion": {"wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            source_text = source.read_text(encoding="utf-8")
            test_text = test.read_text(encoding="utf-8")
            checkpoint = _load_release_progress(repo)

        self.assertEqual(source_text, "before\n")
        self.assertEqual(test_text, "before test\n")
        self.assertEqual(report["reset_recovery"]["finding_id"], "fnd_old")
        self.assertIsNone(checkpoint)
        resume_stopped.assert_not_called()
        self.assertEqual(
            [invocation.args[1] for invocation in json_clawpatch.call_args_list],
            [["clawpatch", "status", "--json"], ["clawpatch", "map", "--json"]],
        )

    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_rebuilt_generation_preserves_source_when_fingerprint_changed_after_checkpoint(
        self,
        _version,
        _processes,
        resume_stopped,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("interrupted repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_old",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            source.write_text("operator edit after interruption\n", encoding="utf-8")
            state = repo / ".clawpatch"
            for directory in ("findings", "patches", "runs", "reports"):
                (state / directory).mkdir(parents=True, exist_ok=True)
            (state / "project.json").write_text(
                json.dumps(
                    {
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "git": {"headSha": head, "currentBranch": branch},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(SafetyError, "exact source changes remain"):
                release_sweep(repo, apply=True, branch="current")
            source_text = source.read_text(encoding="utf-8")
            checkpoint = _load_release_progress(repo)

        self.assertEqual(source_text, "operator edit after interruption\n")
        self.assertIsNotNone(checkpoint)
        resume_stopped.assert_called_once()

    def test_rebuilt_generation_accepts_exact_legacy_v2_checkpoint_owned_files(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            source.write_text("legacy interrupted repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_old",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            progress_path = repo / ".manageroo" / "cache" / "clawpatch-release-progress.json"
            raw = json.loads(progress_path.read_text(encoding="utf-8"))
            raw["version"] = 2
            raw.pop("owned_source_fingerprint")
            progress_path.write_text(json.dumps(raw), encoding="utf-8")
            state = repo / ".clawpatch"
            for directory in ("findings", "patches", "runs", "reports"):
                (state / directory).mkdir(parents=True, exist_ok=True)
            (state / "project.json").write_text(
                json.dumps(
                    {
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "git": {"headSha": head, "currentBranch": branch},
                    }
                ),
                encoding="utf-8",
            )

            progress = _load_release_progress(repo)

            self.assertTrue(_rebuilt_generation_owns_checkpoint_source(repo, progress))

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_rebuilt_generation_retires_empty_checkpoint_after_head_advances(
        self,
        _version,
        _processes,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            stopped_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            _write_release_progress(
                repo,
                finding_id="fnd_deleted_generation",
                branch=branch,
                head_before=stopped_head,
                phase="stopped",
                owned_paths=[],
            )

            state = repo / ".clawpatch"
            (state / "features").mkdir(parents=True)
            (state / "findings").mkdir()
            (state / "patches").mkdir()
            (state / "runs").mkdir()
            (state / "reports").mkdir()
            (state / "project.json").write_text(
                json.dumps(
                    {
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "git": {"headSha": stopped_head, "currentBranch": branch},
                    }
                ),
                encoding="utf-8",
            )
            (state / "features" / "feat_new.json").write_text("{}\n", encoding="utf-8")
            source.write_text("first committed repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "add",
                    "-f",
                    ".clawpatch/project.json",
                    ".clawpatch/features/feat_new.json",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "commit", "-q", "-m", "clawpatch fix"], cwd=repo, check=True)
            second = repo / "second.py"
            second.write_text("second committed repair\n", encoding="utf-8")
            (state / "findings" / "fnd_new.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "second.py"], cwd=repo, check=True)
            subprocess.run(
                ["git", "add", "-f", ".clawpatch/findings/fnd_new.json"],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "commit", "-q", "-m", "clawpatch fix"], cwd=repo, check=True)

            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 0},
                "completion": {"wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            checkpoint = _load_release_progress(repo)

        self.assertIsNone(checkpoint)
        self.assertEqual(
            report["reset_recovery"],
            {
                "finding_id": "fnd_deleted_generation",
                "owned_paths": [],
                "generation": "rebuilt",
            },
        )
        resume_stopped.assert_not_called()
        self.assertEqual(
            [invocation.args[1] for invocation in json_clawpatch.call_args_list],
            [["clawpatch", "status", "--json"], ["clawpatch", "map", "--json"]],
        )

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_clean_descendant_retires_empty_checkpoint_while_preserving_open_finding(
        self,
        _version,
        _processes,
        _gates,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            stopped_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            _write_release_progress(
                repo,
                finding_id="fnd_still_present",
                branch=branch,
                head_before=stopped_head,
                phase="stopped",
                owned_paths=[],
            )
            state = repo / ".clawpatch"
            (state / "findings").mkdir(parents=True)
            (state / "project.json").write_text(
                json.dumps(
                    {
                        "createdAt": "2099-01-01T00:00:00.000Z",
                        "git": {"headSha": stopped_head, "currentBranch": branch},
                    }
                ),
                encoding="utf-8",
            )
            (state / "findings" / "fnd_still_present.json").write_text(
                json.dumps({"findingId": "fnd_still_present", "status": "open"}) + "\n",
                encoding="utf-8",
            )
            source.write_text("committed change\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "add",
                    "-f",
                    ".clawpatch/project.json",
                    ".clawpatch/findings/fnd_still_present.json",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(["git", "commit", "-q", "-m", "later commit"], cwd=repo, check=True)

            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 1},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False, "needs_fresh_review": False}

            report = release_sweep(repo, apply=True, branch="current")
            checkpoint = _load_release_progress(repo)
            finding_preserved = (state / "findings" / "fnd_still_present.json").is_file()

        self.assertIsNone(checkpoint)
        self.assertTrue(finding_preserved)
        self.assertEqual(
            report["reset_recovery"],
            {
                "finding_id": "fnd_still_present",
                "owned_paths": [],
                "generation": "clean-descendant",
            },
        )
        resume_stopped.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_clean_descendant_retires_only_verified_stale_recovery_wrapper(
        self,
        _version,
        _processes,
        _gates,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            later_test = repo / "test_app.py"
            readme = repo / "README.md"
            source.write_text("before\n", encoding="utf-8")
            readme.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            source.write_text("first verified repair\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-q",
                    "-m",
                    "clawpatch-supervise iteration: fnd_one",
                ],
                cwd=repo,
                check=True,
            )
            temporary_commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            subprocess.run(["git", "reset", "--mixed", original_head], cwd=repo, check=True)

            later_test.write_text("later verified repair\n", encoding="utf-8")
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=original_head,
                phase="stopped",
                owned_paths=["app.py", "test_app.py"],
                temporary_commit=temporary_commit,
                last_action=RepairAction.STOP_TERMINAL,
            )
            finding_path = repo / ".clawpatch" / "findings" / "fnd_one.json"
            finding_path.parent.mkdir(parents=True)
            finding_path.write_text(
                json.dumps({"findingId": "fnd_one", "status": "fixed"}) + "\n",
                encoding="utf-8",
            )
            readme.write_text("release work beyond the repair\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "app.py", "test_app.py", "README.md"], cwd=repo, check=True
            )
            subprocess.run(["git", "commit", "-q", "-m", "release"], cwd=repo, check=True)

            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 0},
            ]
            review_all.return_value = {
                "review": {"reviewed": 0, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            checkpoint = _load_release_progress(repo)
            finding_preserved = finding_path.is_file()

        self.assertTrue(report["ok"])
        self.assertIsNone(checkpoint)
        self.assertEqual(
            report["reset_recovery"],
            {
                "finding_id": "fnd_one",
                "owned_paths": [],
                "generation": "clean-descendant",
            },
        )
        resume_stopped.assert_not_called()
        self.assertTrue(finding_preserved)

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._resume_stopped_attempt")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_one_command_clears_completed_stale_checkpoint_from_exact_git_history(
        self,
        _version,
        _processes,
        _gates,
        resume_stopped,
        json_clawpatch,
        review_all,
        next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            stopped_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=stopped_head,
                phase="stopped",
                owned_paths=["app.py"],
            )

            source.write_text("completed Clawpatch repair\n", encoding="utf-8")
            state = repo / ".clawpatch" / "project.json"
            state.parent.mkdir()
            state.write_text('{"step": 1}\n', encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "add", "-f", ".clawpatch/project.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "clawpatch fix"], cwd=repo, check=True)
            state.write_text('{"step": 2}\n', encoding="utf-8")
            subprocess.run(["git", "add", "-f", ".clawpatch/project.json"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "clawpatch fix"], cwd=repo, check=True)

            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 0},
            ]
            review_all.return_value = {
                "review": {"reviewed": 0, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            next_finding.return_value = (None, {"finding": None})
            final_closure.return_value = {"pushed": False}

            report = release_sweep(repo, apply=True, branch="current")
            checkpoint = _load_release_progress(repo)

        self.assertTrue(report["ok"])
        self.assertIsNone(checkpoint)
        resume_stopped.assert_not_called()
        self.assertEqual(
            [invocation.args[1] for invocation in json_clawpatch.call_args_list],
            [["clawpatch", "status", "--json"], ["clawpatch", "map", "--json"]],
        )

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_one_command_preserves_checkpoint_when_commit_contains_unowned_source(
        self,
        _version,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            unrelated = repo / "other.py"
            source.write_text("before\n", encoding="utf-8")
            unrelated.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "other.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            stopped_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=stopped_head,
                phase="stopped",
                owned_paths=["app.py"],
            )
            source.write_text("repair\n", encoding="utf-8")
            unrelated.write_text("unowned change\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "other.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "clawpatch fix"], cwd=repo, check=True)

            with self.assertRaisesRegex(SafetyError, "no longer matches"):
                release_sweep(repo, apply=True, branch="current")
            checkpoint = _load_release_progress(repo)

        self.assertIsNotNone(checkpoint)
        self.assertEqual(checkpoint["head_before"], stopped_head)

    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    def test_workspace_write_revalidation_cannot_silently_change_the_repair(self, json_clawpatch):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("clawpatch repair\n", encoding="utf-8")

            def revalidate_side_effect(*_args, **_kwargs):
                if json_clawpatch.call_count == 1:
                    return {"finding": "fnd_one", "outcome": "uncertain"}
                source.write_text("revalidator changed source\n", encoding="utf-8")
                return {"finding": "fnd_one", "outcome": "fixed"}

            json_clawpatch.side_effect = revalidate_side_effect

            with self.assertRaisesRegex(SafetyError, "must not alter source") as raised:
                _revalidate(repo, "fnd_one", env={}, expected_paths=["app.py"])

        self.assertEqual(raised.exception.outcome, "revalidation-mutated-source")

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_revalidation_source_progress_continues_the_same_finding(self, execute_fix):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()

            def fix_side_effect(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    source.write_text("revalidation source progress\n", encoding="utf-8")
                    raise _UnresolvedFinding(
                        "revalidation changed source",
                        finding_id="fnd_one",
                        outcome="revalidation-mutated-source",
                    )
                source.write_text("completed repair\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect
            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={"finding": {"id": "fnd_one", "status": "open"}},
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )
            final_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            commit_count = subprocess.check_output(
                ["git", "rev-list", "--count", f"{original_head}..{final_head}"],
                cwd=repo,
                text=True,
            ).strip()

        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(continuations, 1)
        self.assertFalse(pushed)
        self.assertEqual(record["commit"], final_head)
        self.assertEqual(commit_count, "1")

    @patch("clawpatch_supervise.clawpatch_release._revalidation_payload")
    def test_failed_revalidation_with_source_progress_is_preserved(self, revalidation_payload):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("clawpatch repair\n", encoding="utf-8")

            def failed_revalidation(*_args, **_kwargs):
                source.write_text("revalidation source progress\n", encoding="utf-8")
                raise SafetyError("phase: Clawpatch command\nexit code: 4")

            revalidation_payload.side_effect = failed_revalidation

            with self.assertRaises(_UnresolvedFinding) as raised:
                _revalidate(repo, "fnd_one", env={}, expected_paths=["app.py"])

        self.assertEqual(
            raised.exception.outcome,
            "revalidation-command-failed-with-source-progress",
        )

    @patch("clawpatch_supervise.clawpatch_release._revalidation_payload")
    def test_false_positive_revalidation_returns_clawpatch_terminal_payload(
        self, revalidation_payload
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("candidate repair\n", encoding="utf-8")
            payload = {
                "finding": "fnd_one",
                "outcome": "false-positive",
                "reasoning": "the pinned dependency proves the original code is correct",
            }
            revalidation_payload.return_value = (
                ["clawpatch", "revalidate", "--finding", "fnd_one", "--json"],
                payload,
                "false-positive",
            )

            result = _revalidate(
                repo,
                "fnd_one",
                env={},
                expected_paths=["app.py"],
            )

        self.assertEqual(result, payload)

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_false_positive_discards_exact_multi_iteration_repair_and_advances(self, execute_fix):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            state_root = root / "state"
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def fix_side_effect(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    source.write_text("incorrect repair\n", encoding="utf-8")
                    outcome = "open"
                else:
                    source.write_text("different incorrect repair\n", encoding="utf-8")
                    outcome = "false-positive"
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {
                            "finding": "fnd_one",
                            "outcome": outcome,
                        },
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect
            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={"finding": {"id": "fnd_one", "status": "open"}},
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=state_root,
                require_project_gates=False,
            )
            final_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
            checkpoint = _load_release_progress(repo, state_root=state_root)

        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(record["revalidation"]["outcome"], "false-positive")
        self.assertTrue(record["false_positive"])
        self.assertEqual(record["discarded_paths"], ["app.py"])
        self.assertEqual(record["files_changed"], [])
        self.assertEqual(record["commit"], "")
        self.assertEqual(continuations, 1)
        self.assertFalse(pushed)
        self.assertEqual(final_head, original_head)
        self.assertEqual(status, "")
        self.assertIsNone(checkpoint)

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_false_positive_discards_exact_first_attempt_repair_and_advances(self, execute_fix):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            state_root = root / "state"
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            original_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def false_positive(*_args, **_kwargs):
                source.write_text("incorrect repair\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {
                            "finding": "fnd_one",
                            "outcome": "false-positive",
                        },
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = false_positive
            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={"finding": {"id": "fnd_one", "status": "open"}},
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=state_root,
                require_project_gates=False,
            )
            final_head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            status = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)

        self.assertEqual(execute_fix.call_count, 1)
        self.assertEqual(record["revalidation"]["outcome"], "false-positive")
        self.assertTrue(record["false_positive"])
        self.assertEqual(record["discarded_paths"], ["app.py"])
        self.assertEqual(record["files_changed"], [])
        self.assertEqual(record["commit"], "")
        self.assertEqual(continuations, 0)
        self.assertFalse(pushed)
        self.assertEqual(final_head, original_head)
        self.assertEqual(status, "")

    @patch("clawpatch_supervise.clawpatch_release._run_clawpatch")
    def test_codex_revalidation_refusal_is_same_finding_provider_failure(self, run_clawpatch):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            source.write_text("new repair\n", encoding="utf-8")
            argv = ["clawpatch", "revalidate", "--finding", "fnd_one", "--json"]
            run_clawpatch.return_value = self.completed(
                argv,
                "ERROR: This content was flagged for possible cybersecurity risk.",
                4,
            )

            with self.assertRaises(_UnresolvedFinding) as raised:
                _revalidate(repo, "fnd_one", env={}, expected_paths=["app.py"])

        self.assertEqual(raised.exception.outcome, "revalidation-provider-failed")

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_revalidation_provider_failure_continues_only_with_new_fix_tree(self, execute_fix):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def fix_side_effect(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    source.write_text("new fix tree before refusal\n", encoding="utf-8")
                    raise _UnresolvedFinding(
                        "Codex revalidation provider failed",
                        finding_id="fnd_one",
                        outcome="revalidation-provider-failed",
                    )
                source.write_text("completed repair\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect
            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={"finding": {"id": "fnd_one", "status": "open"}},
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )

        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(continuations, 1)
        self.assertFalse(pushed)
        self.assertEqual(record["revalidation"]["outcome"], "fixed")

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_failed_revalidation_source_progress_continues_the_same_finding(self, execute_fix):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def fix_side_effect(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    source.write_text("revalidation source progress\n", encoding="utf-8")
                    raise _UnresolvedFinding(
                        "revalidation command failed after changing source",
                        finding_id="fnd_one",
                        outcome="revalidation-command-failed-with-source-progress",
                    )
                source.write_text("completed repair\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["app.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect
            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={"finding": {"id": "fnd_one", "status": "open"}},
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )

        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(continuations, 1)
        self.assertFalse(pushed)
        self.assertEqual(record["revalidation"]["outcome"], "fixed")

    @patch("clawpatch_supervise.clawpatch_release._run_clawpatch")
    def test_nonfix_clawpatch_timeout_stops_without_a_hidden_retry(self, run_clawpatch):
        argv = ["clawpatch", "show", "--finding", "fnd_one", "--json"]
        run_clawpatch.return_value = self.completed(argv, "partial\nTIMEOUT", 124)

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            with self.assertRaisesRegex(SafetyError, "this command is not retried"):
                _must_clawpatch(repo, argv, env={})

        self.assertEqual(run_clawpatch.call_count, 1)

    @patch("clawpatch_supervise.clawpatch_release._run_clawpatch")
    def test_missing_show_finding_stops_immediately_without_transient_retries(self, run_clawpatch):
        argv = ["clawpatch", "show", "--finding", "fnd_old", "--json"]
        run_clawpatch.return_value = self.completed(
            argv,
            "error: finding not found: fnd_old",
            1,
        )

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            with self.assertRaisesRegex(_MissingFinding, "fnd_old"):
                _must_clawpatch(repo, argv, env={})

        self.assertEqual(run_clawpatch.call_count, 1)

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._run")
    def test_fix_requires_matching_finding_and_patch_attempt(self, run, _processes):
        run.return_value = self.completed(
            ["clawpatch", "fix"],
            json.dumps({"finding": "fnd_other", "patchAttempt": "pat_one", "status": "applied"}),
        )
        with self.assertRaisesRegex(SafetyError, "wrong finding"):
            _fix_command(Path("/repo"), ["clawpatch", "fix", "--finding", "fnd_one"])

        run.return_value = self.completed(
            ["clawpatch", "fix"],
            json.dumps({"finding": "fnd_one", "status": "applied"}),
        )
        with self.assertRaisesRegex(SafetyError, "patch-attempt"):
            _fix_command(Path("/repo"), ["clawpatch", "fix", "--finding", "fnd_one"])

    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    def test_apply_refuses_preexisting_source_changes(self, _processes, _version):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            (repo / "app.py").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            (repo / "app.py").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(SafetyError, "pre-existing source changes"):
                release_sweep(repo, apply=True, branch="current")

    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    def test_external_apply_waits_on_preexisting_source_changes(self, _processes, _version):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            (repo / "app.py").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            (repo / "app.py").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(RepositoryBusyError, "waiting without discarding"):
                release_sweep(
                    repo,
                    apply=True,
                    branch="current",
                    integration_mode="external",
                )

            self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "dirty\n")

    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    def test_external_automatic_fresh_waits_before_resetting_dirty_source(
        self, _processes, _version
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            (repo / "app.py").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)
            (repo / "app.py").write_text("dirty\n", encoding="utf-8")

            with self.assertRaisesRegex(RepositoryBusyError, "Automatic fresh review"):
                release_sweep(
                    repo,
                    apply=True,
                    branch="current",
                    integration_mode="external",
                    fresh=True,
                    wait_on_preserved_source=True,
                )

            self.assertEqual((repo / "app.py").read_text(encoding="utf-8"), "dirty\n")

    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    @patch(
        "clawpatch_supervise.clawpatch_release._active_clawpatch_processes",
        return_value=[{"pid": 42}],
    )
    def test_apply_refuses_a_second_clawpatch_process(self, _processes, _version):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            with self.assertRaisesRegex(SafetyError, "already active"):
                release_sweep(repo, apply=True, branch="current")

    def test_apply_refuses_contending_release_sweep_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            context = multiprocessing.get_context("spawn")
            acquired = context.Event()
            release = context.Event()
            owner = context.Process(
                target=_hold_clawpatch_release_lock,
                args=(str(repo), acquired, release),
            )
            try:
                owner.start()
                self.assertTrue(acquired.wait(timeout=5))
                with (
                    patch(
                        "clawpatch_supervise.clawpatch_release._clawpatch_version",
                        return_value="0.7.2",
                    ),
                    patch(
                        "clawpatch_supervise.clawpatch_release._active_clawpatch_processes",
                        return_value=[],
                    ),
                    patch(
                        "clawpatch_supervise.clawpatch_release._json_clawpatch",
                        side_effect=SafetyError("repository sweep lock was ignored"),
                    ),
                    self.assertRaisesRegex(SafetyError, "already active"),
                ):
                    release_sweep(repo, apply=True, branch="current")
            finally:
                release.set()
                owner.join(timeout=5)
                if owner.is_alive():
                    owner.terminate()
                    owner.join(timeout=1)
                self.assertEqual(owner.exitcode, 0)

    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_dry_run_does_not_run_clawpatch(self, _version):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            report = release_sweep(repo, apply=False)
        self.assertTrue(report["ok"])
        self.assertFalse(report["apply"])
        self.assertIn("next/show", report["lifecycle"])

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_zero_open_flow_reaches_final_closure(
        self, _version, _processes, json_clawpatch, final_closure
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 4},
                {"dryRun": True, "wouldReview": 4, "jobs": 4},
                {"reviewed": 4, "findings": 0},
                {"dryRun": True, "wouldReview": 0, "jobs": 4},
                {"finding": None, "status": "open", "next": "clawpatch report --status open"},
            ]
            final_closure.return_value = {"pushed": False}

            report = release_sweep(
                repo,
                apply=True,
                branch="current",
                publish_clawpatch_state=True,
                advance_uncertain=True,
            )

        self.assertEqual(report["open_findings"], 0)
        self.assertTrue(final_closure.call_args.kwargs["publish_clawpatch_state"])
        self.assertFalse(final_closure.call_args.kwargs["resolve_uncertain"])
        self.assertTrue(final_closure.call_args.kwargs["refresh_retained_uncertain"])

    @patch("clawpatch_supervise.clawpatch_release._prepare_fresh_release")
    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_completed_queue_requires_a_fresh_zero_finding_review_generation(
        self,
        _version,
        _processes,
        json_clawpatch,
        execute_fix,
        final_closure,
        prepare_fresh,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
                {"dryRun": True, "wouldReview": 1, "jobs": 1},
                {"reviewed": 1, "findings": 1},
                {"dryRun": True, "wouldReview": 0, "jobs": 1},
                {
                    "finding": {"id": "fnd_one", "status": "open"},
                    "next": "clawpatch show --finding fnd_one",
                },
                {
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                {"finding": None, "status": "open"},
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
                {"dryRun": True, "wouldReview": 1, "jobs": 1},
                {"reviewed": 1, "findings": 0},
                {"dryRun": True, "wouldReview": 0, "jobs": 1},
                {"finding": None, "status": "open"},
            ]
            execute_fix.return_value = (
                {
                    "finding_id": "fnd_one",
                    "files_changed": [],
                    "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                    "commit": "",
                },
                False,
            )
            final_closure.side_effect = [
                {"pushed": False, "needs_fresh_review": True},
                {"pushed": False, "needs_fresh_review": False},
            ]

            report = release_sweep(repo, apply=True, branch="current")

        self.assertEqual(execute_fix.call_count, 1)
        self.assertEqual(final_closure.call_count, 2)
        prepare_fresh.assert_called_once()
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(len(report["review_generations"]), 2)
        self.assertFalse(report["review_generations"][0]["clean"])
        self.assertTrue(report["review_generations"][1]["clean"])

    @patch("clawpatch_supervise.clawpatch_release._prepare_fresh_release")
    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_fresh_review_same_tree_repetition_stops_as_nonconvergent(
        self,
        _version,
        _processes,
        json_clawpatch,
        execute_fix,
        final_closure,
        prepare_fresh,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            first_generation = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
                {"dryRun": True, "wouldReview": 1, "jobs": 1},
                {"reviewed": 1, "findings": 1},
                {"dryRun": True, "wouldReview": 0, "jobs": 1},
                {
                    "finding": {"id": "fnd_one", "status": "open"},
                    "next": "clawpatch show --finding fnd_one",
                },
                {
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                {"finding": None, "status": "open"},
            ]
            second_generation = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
                {"dryRun": True, "wouldReview": 1, "jobs": 1},
                {"reviewed": 1, "findings": 1},
                {"dryRun": True, "wouldReview": 0, "jobs": 1},
                {
                    "finding": {"id": "fnd_two", "status": "open"},
                    "next": "clawpatch show --finding fnd_two",
                },
                {
                    "finding": {"id": "fnd_two", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                {"finding": None, "status": "open"},
            ]
            json_clawpatch.side_effect = first_generation + second_generation
            execute_fix.side_effect = [
                (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": [],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                ),
                (
                    {
                        "finding_id": "fnd_two",
                        "files_changed": [],
                        "revalidation": {"finding": "fnd_two", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                ),
            ]
            final_closure.side_effect = [
                {"pushed": False, "needs_fresh_review": True},
                {"pushed": False, "needs_fresh_review": True},
            ]

            with self.assertRaisesRegex(SafetyError, "did not converge"):
                release_sweep(repo, apply=True, branch="current")

        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(final_closure.call_count, 2)
        prepare_fresh.assert_called_once()

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_lock_cleanup_uses_clawpatch_072_stale_only_contract(
        self, _version, _processes, json_clawpatch, final_closure
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.side_effect = [
                {"activeLocks": 1, "lockFiles": 1, "openFindings": 0},
                {"removed": 1},
                {"features": 0},
                {"dryRun": True, "wouldReview": 0},
                {"finding": None, "status": "open", "next": "clawpatch report --status open"},
            ]
            final_closure.return_value = {"pushed": False}

            release_sweep(repo, apply=True, branch="current")

        self.assertEqual(
            json_clawpatch.call_args_list[1].args[1],
            ["clawpatch", "clean-locks", "--stale-only", "--json"],
        )

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_red_repository_baseline_blocks_before_map_review_or_fix(
        self,
        _version,
        _processes,
        json_clawpatch,
        run_project_gates,
        execute_fix,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.return_value = {
                "activeLocks": 0,
                "lockFiles": 0,
                "openFindings": 96,
            }
            run_project_gates.side_effect = SafetyError(
                "repository baseline validation failed: tests/test_inventory.py"
            )

            with self.assertRaisesRegex(
                SafetyError,
                "repository baseline validation failed: tests/test_inventory.py",
            ):
                release_sweep(repo, apply=True, branch="current")

        run_project_gates.assert_called_once_with(
            repo.resolve(),
            finding_id="baseline-preflight",
            required=True,
        )
        self.assertEqual(json_clawpatch.call_count, 1)
        self.assertEqual(
            json_clawpatch.call_args.args[1],
            ["clawpatch", "status", "--json"],
        )
        execute_fix.assert_not_called()

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch(
        "clawpatch_supervise.clawpatch_release._next_finding",
        return_value=(None, {"finding": None}),
    )
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._run_project_gates")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_green_baseline_that_mutates_source_blocks_before_map_and_review(
        self,
        _version,
        _processes,
        json_clawpatch,
        run_project_gates,
        review_all,
        _next_finding,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            generated = repo / "next-env.d.ts"
            generated.write_text('import "./.next/types/routes.d.ts";\n', encoding="utf-8")
            subprocess.run(["git", "add", "next-env.d.ts"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "tracked source"], cwd=repo, check=True)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            final_closure.return_value = {"needs_fresh_review": False, "pushed": False}

            def mutate_source(*_args, **_kwargs):
                generated.write_text(
                    'import "./.next/dev/types/routes.d.ts";\n',
                    encoding="utf-8",
                )
                return []

            run_project_gates.side_effect = mutate_source

            with self.assertRaisesRegex(
                SafetyError,
                "baseline validation changed project source files: next-env.d.ts",
            ):
                release_sweep(repo, apply=True, branch="current")

        self.assertEqual(json_clawpatch.call_count, 1)
        review_all.assert_not_called()

    def test_project_gate_failure_surfaces_the_exact_command_output(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            (repo / ".manageroo" / "config.toml").write_text(
                "[safety]\n"
                'allowed_programs = ["git"]\n\n'
                "[[verification.gates]]\n"
                'id = "known-failure"\n'
                'kind = "test"\n'
                "required = true\n"
                "timeout_seconds = 60\n"
                'argv = ["git", "rev-parse", "--verify", "refs/heads/does-not-exist"]\n',
                encoding="utf-8",
            )

            with self.assertRaises(SafetyError) as raised:
                _run_project_gates(repo, finding_id="baseline-preflight")

        message = str(raised.exception)
        self.assertIn("known-failure", message)
        self.assertIn("git rev-parse --verify refs/heads/does-not-exist", message)
        self.assertIn("exit code: 128", message)
        self.assertIn("fatal: Needed a single revision", message)

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_fix_flow_uses_only_execute_fix_contract(
        self, _version, _processes, json_clawpatch, execute_fix, final_closure
    ):
        progress_events = []
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 0},
                {"features": 3},
                {"dryRun": True, "wouldReview": 3, "jobs": 3},
                {"reviewed": 3, "findings": 1},
                {"dryRun": True, "wouldReview": 0, "jobs": 3},
                {
                    "finding": {"id": "fnd_one", "status": "open"},
                    "next": "clawpatch show --finding fnd_one",
                },
                {
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": ["python3 -m unittest"],
                    "patchAttempts": [],
                    "next": "clawpatch triage --finding fnd_one --status <status>",
                },
                {"finding": None, "status": "open", "next": "clawpatch report --status open"},
            ]

            def complete_fix(*_args, **_kwargs):
                (repo / "fixed.py").write_text("fixed\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["fixed.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                    },
                    False,
                )

            execute_fix.side_effect = complete_fix
            final_closure.return_value = {"pushed": False}

            report = release_sweep(
                repo,
                apply=True,
                branch="current",
                progress=progress_events.append,
            )

        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(execute_fix.call_args.args[1], "fnd_one")
        self.assertEqual(
            execute_fix.call_args.kwargs["inspected"]["next"],
            "clawpatch triage --finding fnd_one --status <status>",
        )
        self.assertNotIn("publish_clawpatch_state", execute_fix.call_args.kwargs)
        self.assertEqual(progress_events[0]["phase"], "preflight")
        finding_event = next(event for event in progress_events if event["phase"] == "finding")
        self.assertEqual(finding_event["current"], 1)
        self.assertEqual(finding_event["total"], 1)
        self.assertEqual(finding_event["finding_id"], "fnd_one")
        self.assertEqual(finding_event["command"], "clawpatch show --finding fnd_one")
        self.assertEqual(finding_event["inspection"]["finding"]["id"], "fnd_one")

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_open_revalidation_commits_and_reenters_same_finding_without_a_cap(
        self,
        _version,
        _processes,
        json_clawpatch,
        review_all,
        next_finding,
        show_finding,
        execute_fix,
        final_closure,
    ):
        progress_events = []
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 1},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 0},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            queue = {
                "finding": {"id": "fnd_one", "status": "open"},
                "next": "clawpatch show --finding fnd_one",
            }
            next_finding.side_effect = [
                ("fnd_one", queue),
                (None, {"finding": None}),
            ]
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [],
            }
            calls = 0

            def fix_side_effect(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    (repo / "partial.py").write_text("partial\n", encoding="utf-8")
                    outcome = "open"
                    paths = ["partial.py"]
                else:
                    (repo / "final.py").write_text("final\n", encoding="utf-8")
                    outcome = "fixed"
                    paths = ["final.py"]
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": paths,
                        "revalidation": {"finding": "fnd_one", "outcome": outcome},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect
            final_closure.return_value = {"pushed": False}

            report = release_sweep(
                repo,
                apply=True,
                branch="current",
                progress=progress_events.append,
            )

        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(next_finding.call_count, 2)
        self.assertEqual(show_finding.call_count, 1)
        self.assertEqual(report["finding_count"], 1)
        self.assertEqual(len(report["continuations"]), 1)
        self.assertTrue(report["continuations"][0]["temporary_local_commit"])
        self.assertEqual(
            [event["attempt"] for event in progress_events if event["phase"] == "fix"],
            [1, 2],
        )
        self.assertTrue(any(event["phase"] == "continuing" for event in progress_events))
        self.assertFalse(any(event["phase"] == "stopped" for event in progress_events))
        final_closure.assert_called_once()

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_fix_validation_failure_revalidates_saved_repair_before_another_fix(
        self,
        execute_fix,
        revalidate,
        _gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def failed_validation(*_args, **_kwargs):
                (repo / "repair.py").write_text("fixed\n", encoding="utf-8")
                raise _UnresolvedFinding(
                    "error: validation failed after applying fix",
                    finding_id="fnd_one",
                    outcome="fix-validation-failed",
                    failure=classify_clawpatch_failure("fix", 6),
                )

            execute_fix.side_effect = failed_validation
            revalidate.return_value = {
                "finding": "fnd_one",
                "outcome": "fixed",
                "reasoning": "the saved repair passes fresh revalidation",
            }

            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )

            self.assertFalse(pushed)
            self.assertEqual(continuations, 1)
            self.assertEqual(execute_fix.call_count, 1)
            revalidate.assert_called_once_with(
                repo,
                "fnd_one",
                env={},
                expected_paths=[],
                progress=None,
                current="?",
                total="?",
            )
            self.assertEqual(record["revalidation"]["outcome"], "fixed")
            self.assertEqual(record["files_changed"], ["repair.py"])
            self.assertEqual(
                subprocess.check_output(
                    ["git", "show", "-s", "--format=%s", "HEAD"],
                    cwd=repo,
                    text=True,
                ).strip(),
                "clawpatch fix: fnd_one",
            )

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_fix_validation_failure_without_new_source_revalidates_existing_repair(
        self,
        execute_fix,
        revalidate,
        _gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            execute_fix.side_effect = _UnresolvedFinding(
                "error: validation failed after applying fix",
                finding_id="fnd_one",
                outcome="fix-validation-failed",
                failure=classify_clawpatch_failure("fix", 6),
            )
            revalidate.return_value = {
                "finding": "fnd_one",
                "outcome": "fixed",
                "reasoning": "the repair was already present and passes fresh validation",
            }

            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )

            self.assertFalse(pushed)
            self.assertEqual(continuations, 0)
            self.assertEqual(execute_fix.call_count, 1)
            revalidate.assert_called_once_with(
                repo,
                "fnd_one",
                env={},
                expected_paths=[],
                progress=None,
                current="?",
                total="?",
            )
            self.assertEqual(record["revalidation"]["outcome"], "fixed")
            self.assertEqual(record["files_changed"], [])
            self.assertEqual(record["commit"], "")
            self.assertEqual(
                subprocess.check_output(
                    ["git", "rev-list", "--count", "HEAD"], cwd=repo, text=True
                ).strip(),
                "1",
            )
            self.assertIsNone(
                _load_release_progress(repo, state_root=repo / ".manageroo" / "cache")
            )

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_timeout_with_source_progress_revalidates_saved_repair_before_retry(
        self,
        execute_fix,
        revalidate,
        _gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            failure = classify_clawpatch_failure("fix", 124)

            def timed_out_fix(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    (repo / "repair.py").write_text("fixed\n", encoding="utf-8")
                raise _UnresolvedFinding(
                    "fix watchdog expired",
                    finding_id="fnd_one",
                    outcome="timeout",
                    failure=failure,
                )

            execute_fix.side_effect = timed_out_fix
            revalidate.return_value = {
                "finding": "fnd_one",
                "outcome": "fixed",
                "reasoning": "the saved repair already fixes the finding",
            }

            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )

            self.assertFalse(pushed)
            self.assertEqual(continuations, 1)
            self.assertEqual(execute_fix.call_count, 1)
            revalidate.assert_called_once_with(
                repo,
                "fnd_one",
                env={},
                expected_paths=[],
                progress=None,
                current="?",
                total="?",
            )
            self.assertEqual(record["revalidation"]["outcome"], "fixed")
            self.assertEqual(record["files_changed"], ["repair.py"])
            self.assertEqual(
                subprocess.check_output(
                    ["git", "show", "-s", "--format=%s", "HEAD"],
                    cwd=repo,
                    text=True,
                ).strip(),
                "clawpatch fix: fnd_one",
            )
            self.assertIsNone(
                _load_release_progress(repo, state_root=repo / ".manageroo" / "cache")
            )

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_fix_validation_failure_uses_uncertain_evidence_for_next_same_finding_fix(
        self,
        execute_fix,
        revalidate,
        _gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def repair_attempt(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    (repo / "repair.py").write_text("partial\n", encoding="utf-8")
                    (repo / "test_repair.py").write_text("stale assertion\n", encoding="utf-8")
                    raise _UnresolvedFinding(
                        "error: validation failed after applying fix",
                        finding_id="fnd_one",
                        outcome="fix-validation-failed",
                        failure=classify_clawpatch_failure("fix", 6),
                    )
                (repo / "test_repair.py").write_text("corrected assertion\n", encoding="utf-8")
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": ["test_repair.py"],
                        "revalidation": {"finding": "fnd_one", "outcome": "fixed"},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = repair_attempt
            revalidate.return_value = {
                "finding": "fnd_one",
                "outcome": "uncertain",
                "reasoning": "the repair is sound but four stale test assertions still fail",
            }

            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )

        self.assertFalse(pushed)
        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(revalidate.call_count, 1)
        self.assertEqual(continuations, 1)
        self.assertEqual(record["revalidation"]["outcome"], "fixed")

    @patch("clawpatch_supervise.clawpatch_release._run_project_gates", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._revalidate")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_second_validation_failure_without_new_source_revalidates_saved_repair(
        self,
        execute_fix,
        revalidate,
        _gates,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def failed_validation(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    (repo / "repair.py").write_text("fixed\n", encoding="utf-8")
                raise _UnresolvedFinding(
                    "error: validation failed after applying fix",
                    finding_id="fnd_one",
                    outcome="fix-validation-failed",
                    failure=classify_clawpatch_failure("fix", 6),
                )

            execute_fix.side_effect = failed_validation
            revalidate.side_effect = [
                {
                    "finding": "fnd_one",
                    "outcome": "uncertain",
                    "reasoning": "validation environment could not prove the saved repair",
                },
                {
                    "finding": "fnd_one",
                    "outcome": "fixed",
                    "reasoning": "fresh validation proves the existing saved repair",
                },
            ]

            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                require_project_gates=False,
            )

            self.assertFalse(pushed)
            self.assertEqual(continuations, 1)
            self.assertEqual(execute_fix.call_count, 2)
            self.assertEqual(revalidate.call_count, 2)
            self.assertEqual(record["revalidation"]["outcome"], "fixed")
            self.assertEqual(record["files_changed"], ["repair.py"])
            self.assertEqual(
                subprocess.check_output(
                    ["git", "show", "-s", "--format=%s", "HEAD"],
                    cwd=repo,
                    text=True,
                ).strip(),
                "clawpatch fix: fnd_one",
            )
            self.assertIsNone(
                _load_release_progress(repo, state_root=repo / ".manageroo" / "cache")
            )

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch(
        "clawpatch_supervise.clawpatch_release._active_clawpatch_processes",
        return_value=[],
    )
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_open_zero_source_revalidation_informs_second_fix_attempt(
        self,
        _version,
        _processes,
        json_clawpatch,
        review_all,
        next_finding,
        show_finding,
        execute_fix,
        final_closure,
    ):
        progress_events = []
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0, "openFindings": 1},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 1},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            queue = {
                "finding": {"id": "fnd_one", "status": "open"},
                "next": "clawpatch show --finding fnd_one",
            }
            next_finding.side_effect = [
                ("fnd_one", queue),
                (None, {"finding": None}),
            ]
            show_finding.return_value = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": [],
                "patchAttempts": [],
            }
            calls = 0

            def fix_side_effect(*_args, **_kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    paths = []
                    outcome = "open"
                else:
                    (repo / "repair.py").write_text("fixed\n", encoding="utf-8")
                    paths = ["repair.py"]
                    outcome = "fixed"
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": paths,
                        "revalidation": {"finding": "fnd_one", "outcome": outcome},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect
            final_closure.return_value = {"pushed": False}

            report = release_sweep(
                repo,
                apply=True,
                branch="current",
                progress=progress_events.append,
            )

        self.assertEqual(execute_fix.call_count, 2)
        self.assertEqual(report["finding_count"], 1)
        self.assertTrue(
            any(
                event.get("evidence_retry") == 1
                for event in progress_events
                if event["phase"] == "continuing"
            )
        )
        self.assertFalse(any(event["phase"] == "stopped" for event in progress_events))
        final_closure.assert_called_once()

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_open_zero_source_revalidation_retries_after_checkpointed_repair(
        self, execute_fix
    ):
        progress_events = []
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()

            def fix_side_effect(*_args, **_kwargs):
                if execute_fix.call_count == 1:
                    (repo / "repair.py").write_text("partial\n", encoding="utf-8")
                    paths = ["repair.py"]
                    outcome = "open"
                elif execute_fix.call_count == 2:
                    paths = []
                    outcome = "open"
                else:
                    (repo / "repair.py").write_text("fixed\n", encoding="utf-8")
                    paths = ["repair.py"]
                    outcome = "fixed"
                return (
                    {
                        "finding_id": "fnd_one",
                        "files_changed": paths,
                        "revalidation": {"finding": "fnd_one", "outcome": outcome},
                        "commit": "",
                    },
                    False,
                )

            execute_fix.side_effect = fix_side_effect

            record, pushed, continuations = _process_finding_until_fixed(
                repo,
                "fnd_one",
                inspected={
                    "finding": {"id": "fnd_one", "status": "open"},
                    "validation": [],
                    "patchAttempts": [],
                },
                env={},
                push_mode="none",
                branch=branch,
                pushed=False,
                state_root=repo / ".manageroo" / "cache",
                progress=progress_events.append,
                require_project_gates=False,
            )

        self.assertFalse(pushed)
        self.assertEqual(execute_fix.call_count, 3)
        self.assertEqual(continuations, 1)
        self.assertEqual(record["revalidation"]["outcome"], "fixed")
        self.assertEqual(record["files_changed"], ["repair.py"])
        self.assertTrue(
            any(
                event.get("evidence_retry") == 1
                for event in progress_events
                if event["phase"] == "continuing"
            )
        )

    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_open_zero_source_revalidation_has_bounded_evidence_retries(self, execute_fix):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo, text=True
            ).strip()
            execute_fix.return_value = (
                {
                    "finding_id": "fnd_one",
                    "files_changed": [],
                    "revalidation": {"finding": "fnd_one", "outcome": "open"},
                    "commit": "",
                },
                False,
            )

            with self.assertRaisesRegex(_UnresolvedFinding, "no source changes"):
                _process_finding_until_fixed(
                    repo,
                    "fnd_one",
                    inspected={
                        "finding": {"id": "fnd_one", "status": "open"},
                        "validation": [],
                        "patchAttempts": [],
                    },
                    env={},
                    push_mode="none",
                    branch=branch,
                    pushed=False,
                    state_root=repo / ".manageroo" / "cache",
                    require_project_gates=False,
                )

            checkpoint = _load_release_progress(repo)

        self.assertEqual(execute_fix.call_count, 3)
        self.assertEqual(checkpoint["phase"], "stopped")
        self.assertEqual(checkpoint["owned_paths"], [])

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_missing_selected_finding_stops_without_remap_review_or_queue_advance(
        self,
        _version,
        _processes,
        json_clawpatch,
        review_all,
        next_finding,
        show_finding,
        execute_fix,
        final_closure,
    ):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 1},
                "completion": {"wouldReview": 0},
            }
            next_finding.return_value = (
                "fnd_old",
                {"finding": {"id": "fnd_old", "status": "open"}},
            )
            show_finding.side_effect = _MissingFinding("finding not found", finding_id="fnd_old")

            with self.assertRaisesRegex(SafetyError, "stopped without remapping"):
                release_sweep(repo, apply=True, branch="current")

        execute_fix.assert_not_called()
        final_closure.assert_not_called()
        self.assertEqual(next_finding.call_count, 1)
        self.assertEqual(review_all.call_count, 1)
        self.assertEqual(
            [call.args[1] for call in json_clawpatch.call_args_list],
            [
                ["clawpatch", "status", "--json"],
                ["clawpatch", "map", "--json"],
            ],
        )

    @patch("clawpatch_supervise.clawpatch_release._final_closure")
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    @patch("clawpatch_supervise.clawpatch_release._show_finding")
    @patch("clawpatch_supervise.clawpatch_release._next_finding")
    @patch("clawpatch_supervise.clawpatch_release._review_all_features")
    @patch("clawpatch_supervise.clawpatch_release._json_clawpatch")
    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._clawpatch_version", return_value="0.7.2")
    def test_failed_fix_retries_only_until_no_progress_then_stops_without_queue_advance(
        self,
        _version,
        _processes,
        json_clawpatch,
        review_all,
        next_finding,
        show_finding,
        execute_fix,
        final_closure,
    ):
        progress_events = []
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "source"], cwd=repo, check=True)
            json_clawpatch.side_effect = [
                {"activeLocks": 0, "lockFiles": 0},
                {"features": 1},
            ]
            review_all.return_value = {
                "review": {"reviewed": 1, "findings": 1},
                "completion": {"dryRun": True, "wouldReview": 0},
            }
            queue = {
                "finding": {"id": "fnd_one", "status": "open"},
                "next": "clawpatch show --finding fnd_one",
            }
            inspected = {
                "finding": {"id": "fnd_one", "status": "open"},
                "validation": ["python3 -m unittest"],
                "patchAttempts": [],
            }
            next_finding.return_value = ("fnd_one", queue)
            show_finding.return_value = inspected

            def fail_once(*_args, **_kwargs):
                source.write_text("clawpatch-owned failed repair\n", encoding="utf-8")
                raise _UnresolvedFinding(
                    "validation stayed open",
                    finding_id="fnd_one",
                    outcome="fix-validation-failed",
                )

            execute_fix.side_effect = fail_once

            with (
                patch(
                    "clawpatch_supervise.clawpatch_release._revalidate",
                    return_value={"finding": "fnd_one", "outcome": "open"},
                ),
                self.assertRaisesRegex(SafetyError, "stopped"),
            ):
                release_sweep(
                    repo,
                    apply=True,
                    branch="current",
                    progress=progress_events.append,
                )

            checkpoint = _load_release_progress(repo)
            source_text = source.read_text(encoding="utf-8")
            stash_list = subprocess.run(
                ["git", "stash", "list"],
                cwd=repo,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout

        self.assertEqual(execute_fix.call_count, 2)
        final_closure.assert_not_called()
        self.assertEqual(next_finding.call_count, 1)
        self.assertEqual(source_text, "clawpatch-owned failed repair\n")
        self.assertEqual(stash_list, "")
        self.assertEqual(checkpoint["phase"], "stopped")
        self.assertEqual(checkpoint["owned_paths"], ["app.py"])
        self.assertNotIn(
            "triage",
            [
                argument
                for invocation in json_clawpatch.call_args_list
                for argument in invocation.args[1]
            ],
        )
        self.assertEqual(
            [event["attempt"] for event in progress_events if event["phase"] == "fix"],
            [1, 2],
        )

    def test_release_progress_is_durable_and_bound_to_the_current_finding(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch="main",
                head_before="abc123",
                phase="fix",
                owned_paths=[],
            )

            progress = _load_release_progress(repo)

        self.assertEqual(progress["finding_id"], "fnd_one")
        self.assertEqual(progress["branch"], "main")
        self.assertEqual(progress["head_before"], "abc123")
        self.assertEqual(progress["owned_paths"], [])
        self.assertEqual(progress["phase"], "fix")

    @patch("clawpatch_supervise.clawpatch_release._active_clawpatch_processes", return_value=[])
    @patch("clawpatch_supervise.clawpatch_release._execute_fix")
    def test_transient_no_progress_stop_is_durable_and_service_retryable(
        self,
        execute_fix,
        _processes,
    ):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "repo"
            repo.mkdir()
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=repo, text=True
            ).strip()
            state_root = root / "state"
            failure = classify_clawpatch_failure("fix", 124)
            execute_fix.side_effect = _UnresolvedFinding(
                "fix watchdog expired",
                finding_id="fnd_one",
                outcome="timeout",
                failure=failure,
            )

            with self.assertRaises(_UnresolvedFinding) as raised:
                _process_finding_until_fixed(
                    repo,
                    "fnd_one",
                    inspected={"finding": {"id": "fnd_one", "status": "open"}},
                    env={},
                    push_mode="none",
                    branch=branch,
                    pushed=False,
                    state_root=state_root,
                )
            checkpoint = _load_release_progress(repo, state_root=state_root)

        self.assertEqual(raised.exception.repair_action, RepairAction.STOP_TRANSIENT)
        self.assertEqual(checkpoint["last_action"], RepairAction.STOP_TRANSIENT.value)
        self.assertEqual(execute_fix.call_count, 1)

    def test_only_transient_checkpoint_can_resume_a_failed_zero_file_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            self.init_repo(repo)
            branch = subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=repo, text=True
            ).strip()
            head = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=repo, text=True
            ).strip()
            inspected = {
                "finding": {"id": "fnd_one", "status": "open"},
                "patchAttempts": [
                    {
                        "patchAttemptId": "pat_timeout",
                        "status": "failed",
                        "findingIds": ["fnd_one"],
                        "filesChanged": [],
                        "git": {"baseSha": head},
                    }
                ],
            }
            transient = _write_release_progress(
                repo,
                finding_id="fnd_one",
                branch=branch,
                head_before=head,
                phase="stopped",
                owned_paths=[],
                last_action=RepairAction.STOP_TRANSIENT,
            )
            terminal = dict(transient, last_action=RepairAction.STOP_TERMINAL.value)

            resumed = _checkpoint_unapplied_attempt(repo, transient, env={}, inspected=inspected)
            blocked = _checkpoint_unapplied_attempt(repo, terminal, env={}, inspected=inspected)

        self.assertEqual(resumed["patch_attempts"], ["pat_timeout"])
        self.assertIsNone(blocked)


if __name__ == "__main__":
    unittest.main()
