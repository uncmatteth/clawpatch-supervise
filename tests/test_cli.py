from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from clawpatch_supervise import __version__
from clawpatch_supervise.clawpatch_external import (
    _heartbeat_lines,
    _render_event,
    _run_state_query,
    _terminal_safe,
    main,
)
from clawpatch_supervise.clawpatch_protocol import RepairAction, classify_clawpatch_failure
from clawpatch_supervise.clawpatch_release import (
    ClawpatchCommandFailure,
    ClawpatchStop,
    _release_clawpatch_env,
)
from clawpatch_supervise.errors import RepositoryBusyError, SafetyError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ExternalClawpatchSupervisorTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows command shim test")
    def test_state_query_launches_clawpatch_cmd_from_path_with_spaces(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repository"
            repo.mkdir()
            tool_directory = Path(temp) / "tools with spaces"
            tool_directory.mkdir()
            shim = tool_directory / "clawpatch.cmd"
            shim.write_text(
                "@echo off\r\n"
                'echo {"openFindings":45,"activeLocks":0,"lockFiles":0}\r\n',
                encoding="ascii",
            )
            path = os.pathsep.join((str(tool_directory), os.environ.get("PATH", "")))

            with patch.dict(os.environ, {"PATH": path}):
                result = _run_state_query(repo, ["clawpatch", "status", "--json"])

        self.assertEqual(result["openFindings"], 45)

    def test_state_query_parses_stdout_despite_stderr_diagnostics(self):
        repo = Path("/tmp/example-repository")
        argv = ["clawpatch", "status", "--json"]
        completed = SimpleNamespace(
            exit_code=0,
            stdout='{"openFindings": 0}',
            stderr="warning: optional metadata unavailable\n",
        )

        with patch(
            "clawpatch_supervise.clawpatch_external.CommandRunner.run",
            return_value=completed,
        ) as run:
            result = _run_state_query(repo, argv)

        self.assertEqual(result, {"openFindings": 0})
        run.assert_called_once_with(
            argv,
            cwd=repo,
            timeout_seconds=120,
            env=_release_clawpatch_env(trusted_host_codex_sandbox_bypass=False),
            kill_process_group=True,
        )

    @patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://production.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "ambient-reset-enabled",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "NPM_TOKEN": "registry-secret",
            "SSH_AUTH_SOCK": "/tmp/host-agent.sock",
            "CLAWPATCH_TEST_AMBIENT_SENTINEL": "must-not-be-inherited",
        },
    )
    def test_state_query_uses_exact_sanitized_release_environment(self):
        validation_overrides = {
            "CLAWPATCH_TEST_VALIDATION_OVERRIDE": "owned-validation-override"
        }
        expected = _release_clawpatch_env(
            trusted_host_codex_sandbox_bypass=False,
            child_env_overrides=validation_overrides,
        )

        result = _run_state_query(
            Path.cwd(),
            [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps(dict(os.environ), sort_keys=True))",
            ],
            preflight_env_overrides=validation_overrides,
        )

        sensitive_environment = {
            "DATABASE_URL": "postgresql://production.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "ambient-reset-enabled",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "NPM_TOKEN": "registry-secret",
            "SSH_AUTH_SOCK": "/tmp/host-agent.sock",
        }
        for name, value in sensitive_environment.items():
            self.assertNotIn(name, expected)
            self.assertNotIn(name, result)
            self.assertNotIn(value, json.dumps(expected))
            self.assertNotIn(value, json.dumps(result))
        self.assertNotIn("CLAWPATCH_TEST_AMBIENT_SENTINEL", expected)
        self.assertNotIn("CLAWPATCH_TEST_AMBIENT_SENTINEL", result)
        self.assertEqual(
            result["CLAWPATCH_TEST_VALIDATION_OVERRIDE"],
            "owned-validation-override",
        )
        self.assertEqual(result.get("PATH"), os.environ.get("PATH"))
        self.assertEqual(result, expected)

    def test_state_query_failure_reports_bounded_stdout_and_stderr(self):
        repo = Path("/tmp/example-repository")
        argv = ["clawpatch", "status", "--json"]
        completed = SimpleNamespace(
            exit_code=2,
            stdout="discarded stdout\n" + ("o" * 4000),
            stderr="discarded stderr\n" + ("e" * 4000),
        )

        with (
            patch(
                "clawpatch_supervise.clawpatch_external.CommandRunner.run",
                return_value=completed,
            ),
            self.assertRaises(SafetyError) as raised,
        ):
            _run_state_query(repo, argv)

        message = str(raised.exception)
        self.assertIn("stdout:\n" + ("o" * 4000), message)
        self.assertIn("stderr:\n" + ("e" * 4000), message)
        self.assertNotIn("discarded stdout", message)
        self.assertNotIn("discarded stderr", message)

    def test_state_query_failure_redacts_bounded_stdout_and_stderr(self):
        repo = Path("/tmp/example-repository")
        argv = ["clawpatch", "status", "--json"]
        completed = SimpleNamespace(
            exit_code=2,
            stdout=("o" * 8000) + "\ntoken=stdout-secret",
            stderr=(
                ("e" * 8000)
                + "\nAuthorization: Bearer bearer-secret"
                + "\npassword=stderr-secret"
            ),
        )

        with (
            patch(
                "clawpatch_supervise.clawpatch_external.CommandRunner.run",
                return_value=completed,
            ),
            self.assertRaises(SafetyError) as raised,
        ):
            _run_state_query(repo, argv)

        message = str(raised.exception)
        stdout_diagnostic, stderr_diagnostic = message.split("\nstderr:\n", 1)
        stdout_diagnostic = stdout_diagnostic.split("\nstdout:\n", 1)[1]
        self.assertLessEqual(len(stdout_diagnostic), 4000)
        self.assertLessEqual(len(stderr_diagnostic), 4000)
        self.assertIn("token=<REDACTED>", stdout_diagnostic)
        self.assertIn("Authorization: Bearer <REDACTED>", stderr_diagnostic)
        self.assertIn("password=<REDACTED>", stderr_diagnostic)
        for secret in ("stdout-secret", "bearer-secret", "stderr-secret"):
            self.assertNotIn(secret, message)

    def test_version_is_available_without_running_clawpatch(self):
        output = StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"clawpatch-supervise {__version__}")

    def test_plain_command_rejects_non_positive_or_non_finite_retry_delays(self):
        def unexpected_preflight(_repo: Path):
            raise AssertionError("preflight must not run for an invalid retry delay")

        for value in ("-1", "0", "nan", "inf"):
            with self.subTest(value=value):
                error = StringIO()
                with self.assertRaises(SystemExit) as raised, redirect_stderr(error):
                    main(
                        ["--retry-seconds", value],
                        ensure_repository_idle=unexpected_preflight,
                    )

                self.assertEqual(raised.exception.code, 2)
                self.assertIn(
                    "--retry-seconds must be a finite positive number",
                    error.getvalue(),
                )

    def test_plain_command_accepts_a_finite_positive_retry_delay(self):
        with redirect_stdout(StringIO()):
            code = main(["--retry-seconds", "0.5", "--print-state-path"])

        self.assertEqual(code, 0)

    def test_distribution_version_is_derived_from_package_version(self):
        manifest = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertNotIn("version", manifest["project"])
        self.assertIn("version", manifest["project"]["dynamic"])
        self.assertEqual(
            manifest["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "clawpatch_supervise.__version__"},
        )

    def test_doctor_reports_portable_runtime_without_starting_queue(self):
        output = StringIO()
        report = {
            "ready": True,
            "platform": "win32",
            "provider": "codex",
            "windowsCodexSandbox": "ready",
        }
        with (
            patch(
                "clawpatch_supervise.clawpatch_external.runtime_doctor",
                return_value=(report, {"PATH": "C:\\working-codex"}),
            ) as doctor,
            redirect_stdout(output),
        ):
            code = main(["doctor", "--repo", "."], heartbeat_seconds=0)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), report)
        doctor.assert_called_once_with(Path(".").resolve())

    def test_doctor_reports_repository_resolution_failure(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as temp:
            loop = Path(temp) / "loop"
            try:
                loop.symlink_to(loop)
            except (NotImplementedError, OSError) as exc:
                self.skipTest(f"file symlinks are unavailable: {exc}")

            with redirect_stdout(output):
                code = main(["doctor", "--repo", str(loop)], heartbeat_seconds=0)

        self.assertEqual(code, 2)
        self.assertIn("NOT READY: Could not resolve repository path", output.getvalue())

    def test_same_finding_continuation_says_the_repair_is_still_broken(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "continuing",
                    "current": 10,
                    "total": 12,
                    "commit": "7058b5964e2ef6d51472dbf6c4346342dcfef52a",
                }
            ),
            "\n[10/12] MOTHERFUCKER, SHIT'S STILL FUCKED. "
            "CONTINUING THE SAME FUCKING FINDING. 🤬🦶💥\n"
            "commit: 7058b5964e2ef6d51472dbf6c4346342dcfef52a",
        )

    def test_false_positive_cleanup_is_blunt_and_exact(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "false-positive",
                    "current": 3,
                    "total": 9,
                    "finding_id": "fnd_bogus",
                    "detail": "restored exact supervisor-owned paths",
                }
            ),
            "\n[3/9] FALSE-POSITIVE — 🙄🗑️ BOGUS BUG. "
            "THROW OUT ONLY OUR SHIT AND KEEP MOVING\n"
            "finding: fnd_bogus\n"
            "detail: restored exact supervisor-owned paths",
        )

    def test_submodule_exclusion_names_the_exact_excluded_path(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "submodule-exclusion",
                    "current": "?",
                    "total": "?",
                    "detail": "lib/openzeppelin-contracts",
                }
            ),
            "\n[?/?] SUBMODULE EXCLUSION — "
            "🚧🙅 NOT TOUCHING SOMEBODY ELSE'S SHIT\n"
            "excluded: lib/openzeppelin-contracts",
        )

    def test_git_sync_progress_explains_the_automatic_fast_forward(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "git-sync",
                    "current": "?",
                    "total": "?",
                    "command": "git merge --ff-only abc123",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            ),
            "\n[?/?] GIT SYNC (attempt 1/1) — 🔄📦 CATCHING UP AUTOMATICALLY\n"
            "$ git merge --ff-only abc123",
        )

    def test_finding_rendering_escapes_terminal_controls(self):
        rendered = _render_event(
            {
                "phase": "finding",
                "current": 1,
                "total": 1,
                "command": "clawpatch show\x1b]52;c;payload\x07\nCOMPLETE: forged",
                "inspection": {
                    "finding": {
                        "title": "hostile\rtitle",
                        "id": "fnd_one",
                        "severity": "medium",
                        "category": "security",
                        "evidence": [
                            {
                                "path": "src/evil\x1b[2J.py\nSTOPPED: forged",
                                "startLine": 4,
                                "symbol": "run\x9b31m",
                            }
                        ],
                        "reproduction": "ring\x07bell",
                    },
                    "validation": ["python -m unittest\nCOMPLETE: forged"],
                },
            }
        )

        self.assertIn(r"$ clawpatch show\x1b]52;c;payload\x07\nCOMPLETE: forged", rendered)
        self.assertIn(r"title: hostile\rtitle", rendered)
        self.assertIn(r"- src/evil\x1b[2J.py\nSTOPPED: forged:4 (run\x9b31m)", rendered)
        self.assertIn(r"ring\x07bell", rendered)
        self.assertIn(r"- python -m unittest\nCOMPLETE: forged", rendered)
        self.assertNotIn("\nCOMPLETE: forged", rendered)
        self.assertNotIn("\nSTOPPED: forged", rendered)
        for control in ("\x07", "\r", "\x1b", "\x9b"):
            self.assertNotIn(control, rendered)

    def test_terminal_safe_escapes_unicode_format_controls(self):
        bidi_controls = "".join(chr(codepoint) for codepoint in range(0x202A, 0x202F))
        bidi_controls += "".join(chr(codepoint) for codepoint in range(0x2066, 0x206A))

        self.assertEqual(
            _terminal_safe(f"café 猫 {bidi_controls}\U000e0001"),
            "café 猫 "
            r"\u202a\u202b\u202c\u202d\u202e"
            r"\u2066\u2067\u2068\u2069\U000e0001",
        )
        rendered = _render_event(
            {
                "phase": "finding",
                "current": 1,
                "total": 1,
                "inspection": {
                    "finding": {
                        "title": "safe.py\u202egnorw",
                        "id": "fnd_one",
                        "severity": "low",
                        "category": "security",
                    }
                },
            }
        )

        self.assertIn(r"title: safe.py\u202egnorw", rendered)
        self.assertNotIn("\u202e", rendered)

    def test_general_event_rendering_escapes_controls_in_all_field_shapes(self):
        events = [
            {
                "phase": "review",
                "current": "1\r9",
                "total": 2,
                "attempt": "1\x1b",
                "max_attempts": "2\x07",
                "command": "clawpatch review\nCOMPLETE: forged",
            },
            {
                "phase": "false-positive",
                "current": 1,
                "total": 2,
                "finding_id": "fnd\x1b[2J",
                "detail": "restored\nSTOPPED: forged",
            },
            {
                "phase": "stopped",
                "current": 1,
                "total": 2,
                "finding_id": "fnd_one",
                "outcome": "unsafe\rCOMPLETE: forged",
                "owned_paths": ["src/evil\x1b]52;c;payload\x07.py"],
            },
            {
                "phase": "unknown",
                "current": 1,
                "total": 2,
                "detail": "working\x9b31m\nCOMPLETE: forged",
            },
        ]

        for event in events:
            with self.subTest(phase=event["phase"]):
                rendered = _render_event(event)
                self.assertNotIn("\nCOMPLETE: forged", rendered)
                self.assertNotIn("\nSTOPPED: forged", rendered)
                for control in ("\x07", "\r", "\x1b", "\x9b"):
                    self.assertNotIn(control, rendered)

    def test_heartbeat_rendering_escapes_controls(self):
        attack = "value\nforged\r\t\x1b[2J\x1b]0;spoofed\x07\x9b31m"
        escaped_attack = r"value\nforged\r\t\x1b[2J\x1b]0;spoofed\x07\x9b31m"
        snapshot = {
            "phase": "review",
            "current": 1,
            "total": 2,
            "attempt": 1,
            "max_attempts": 2,
            "finding_id": "fnd_example",
            "command": "clawpatch review",
            "changed": 100,
        }

        for field in (
            "phase",
            "current",
            "total",
            "attempt",
            "max_attempts",
            "finding_id",
            "command",
        ):
            with self.subTest(field=field):
                lines = _heartbeat_lines(
                    {**snapshot, field: attack},
                    watchdog_seconds=900,
                    now=105,
                )
                rendered = "\n".join(lines)

                self.assertIn(escaped_attack, rendered)
                for control in ("\nforged", "\x07", "\r", "\t", "\x1b", "\x9b"):
                    self.assertNotIn(control, rendered)

    def test_phase_event_precedes_heartbeat_for_that_phase(self):
        class DelayedPhaseOutput:
            def __init__(self):
                self._parts: list[str] = []
                self._lock = threading.Lock()
                self._delayed = False
                self._review_heartbeat_written = threading.Event()

            def write(self, value: str) -> int:
                if "[1/1] still running: review" in value:
                    with self._lock:
                        self._parts.append(value)
                    self._review_heartbeat_written.set()
                    return len(value)
                with self._lock:
                    delay = "\n[1/1] REVIEW" in value and not self._delayed
                    if delay:
                        self._delayed = True
                if delay:
                    self._review_heartbeat_written.wait(0.1)
                with self._lock:
                    self._parts.append(value)
                return len(value)

            def flush(self) -> None:
                pass

            def getvalue(self) -> str:
                with self._lock:
                    return "".join(self._parts)

        @contextmanager
        def fake_provision(_repo: Path, *, progress, temporary_root: Path):
            yield {}

        def fake_sweep(_repo: Path, **kwargs):
            kwargs["progress"](
                {
                    "phase": "review",
                    "current": 1,
                    "total": 1,
                    "command": "clawpatch review --all --json",
                }
            )
            time.sleep(0.03)
            return {
                "ok": True,
                "finding_count": 0,
                "open_findings": 0,
                "git_head": "abc123",
            }

        output = DelayedPhaseOutput()
        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            patch("sys.stdout", output),
        ):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                provision_validation_environment=fake_provision,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0.001,
            )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("still running: review", rendered)
        self.assertLess(
            rendered.index("[1/1] REVIEW"),
            rendered.index("[1/1] still running: review"),
        )

    def test_cleanup_path_output_escapes_controls(self):
        output = StringIO()
        report = SimpleNamespace(
            root=Path("/tmp/cleanup\nCOMPLETE: forged"),
            entries=[
                SimpleNamespace(
                    status="retained",
                    path=Path("/tmp/run\r\t\x1b\x9b"),
                    bytes=12,
                )
            ],
            removed=0,
            removed_bytes=0,
        )
        with (
            patch(
                "clawpatch_supervise.clawpatch_external.cleanup_owned_runs",
                return_value=report,
            ),
            redirect_stdout(output),
        ):
            code = main(["cleanup", "--dry-run"])

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(r"cleanup\nCOMPLETE: forged", rendered)
        self.assertIn(r"run\r\t\x1b\x9b", rendered)
        self.assertNotIn("\nCOMPLETE: forged", rendered)
        for control in ("\r", "\t", "\x1b", "\x9b"):
            self.assertNotIn(control, rendered)

    def test_startup_repository_path_output_escapes_controls(self):
        if os.name == "nt":
            self.skipTest("Windows rejects control characters in path components")

        @contextmanager
        def fake_provision(_repo: Path, *, progress, temporary_root: Path):
            yield {}

        def fake_sweep(_repo: Path, **_kwargs):
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo\nCOMPLETE: forged\r\t\x1b\x9b"
            repo.mkdir()
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    ["--repo", str(repo), "--branch", "current\x07"],
                    run_sweep=fake_sweep,
                    provision_validation_environment=fake_provision,
                    ensure_repository_idle=lambda _repo: None,
                    heartbeat_seconds=0,
                    cleanup_root=Path(temp) / "cleanup",
                )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(r"repo\nCOMPLETE: forged\r\t\x1b\x9b", rendered)
        self.assertIn(r"branch=current\x07", rendered)
        self.assertNotIn("\nCOMPLETE: forged", rendered)
        for control in ("\x07", "\r", "\t", "\x1b", "\x9b"):
            self.assertNotIn(control, rendered)

    def test_print_state_path_is_read_only_and_skips_preflight(self):
        repo = Path("/tmp/example-repository")
        expected = Path("/tmp/example-state\nCOMPLETE: forged")
        output = StringIO()
        with (
            patch(
                "clawpatch_supervise.clawpatch_external.external_state_root",
                return_value=expected,
            ) as state_root,
            redirect_stdout(output),
        ):
            result = main(["--repo", str(repo), "--print-state-path"])

        self.assertEqual(result, 0)
        state_root.assert_called_once_with(repo.expanduser().resolve())
        self.assertEqual(output.getvalue().strip(), _terminal_safe(expected))

    def test_python_validation_environment_lifecycle_is_visible(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "validation-environment-start",
                    "current": "?",
                    "total": "?",
                    "command": "create disposable Python validation environment",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            ),
            "\n[?/?] VALIDATION ENVIRONMENT START (attempt 1/1)\n"
            "$ create disposable Python validation environment",
        )
        self.assertEqual(
            _render_event(
                {
                    "phase": "validation-environment-cleanup",
                    "current": "?",
                    "total": "?",
                    "detail": "disposable Python validation environment removed",
                }
            ),
            "\n[?/?] VALIDATION ENVIRONMENT CLEANUP\n"
            "$ disposable Python validation environment removed",
        )

    def test_validation_service_is_visible_and_scoped_to_clawpatch_children(self):
        calls = []
        lifecycle = []

        @contextmanager
        def fake_provision(repo: Path, *, progress, temporary_root: Path):
            lifecycle.append(("start", repo, temporary_root))
            progress(
                {
                    "phase": "validation-service-start",
                    "current": "?",
                    "total": "?",
                    "command": "create owned disposable PostgreSQL validation database",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
            try:
                yield {
                    "TEST_DATABASE_URL": "postgresql://127.0.0.1:49152/test",
                    "BTT_ALLOW_DATABASE_RESET": "true",
                }
            finally:
                lifecycle.append(("cleanup", repo))

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {
                "ok": True,
                "finding_count": 0,
                "open_findings": 0,
                "git_head": "abc123",
            }

        def fake_idle(repo: Path):
            lifecycle.append(("idle", repo))
            return {"PATH": "C:\\working-codex-bin"}

        output = StringIO()
        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(output),
        ):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                provision_validation_environment=fake_provision,
                ensure_repository_idle=fake_idle,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertEqual([item[0] for item in lifecycle], ["idle", "start", "cleanup"])
        child_env = calls[0][1]["child_env_overrides"]
        self.assertEqual(child_env["TEST_DATABASE_URL"], "postgresql://127.0.0.1:49152/test")
        self.assertEqual(child_env["BTT_ALLOW_DATABASE_RESET"], "true")
        self.assertEqual(child_env["PATH"], "C:\\working-codex-bin")
        for variable in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(child_env[variable], str(lifecycle[1][2]))
        self.assertIn("VALIDATION SERVICE START", output.getvalue())
        self.assertLess(
            output.getvalue().index("PROCESS PREFLIGHT"),
            output.getvalue().index("VALIDATION SERVICE START"),
        )
        self.assertEqual(output.getvalue().count("PROCESS PREFLIGHT"), 1)

    @patch("clawpatch_supervise.clawpatch_external._source_paths", return_value=[])
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=True)
    def test_existing_state_queries_receive_preflight_environment_overrides(
        self, _state_exists, _source_paths
    ):
        preflight_env = {
            "PATH": "/preflight/clawpatch/bin",
            "CLAWPATCH_PREFLIGHT_SENTINEL": "ready",
        }
        completed = [
            SimpleNamespace(
                exit_code=0,
                stdout='{"openFindings": 0, "activeLocks": 0, "lockFiles": 0}',
                stderr="",
            ),
            SimpleNamespace(exit_code=0, stdout='{"total": 0}', stderr=""),
        ]

        @contextmanager
        def fake_provision(_repo: Path, *, progress, temporary_root: Path):
            yield {}

        def fake_sweep(_repo: Path, **_kwargs):
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with tempfile.TemporaryDirectory() as temp:
            with (
                patch(
                    "clawpatch_supervise.clawpatch_external.CommandRunner.run",
                    side_effect=completed,
                ) as run,
                redirect_stdout(StringIO()),
            ):
                code = main(
                    ["--repo", "."],
                    run_sweep=fake_sweep,
                    provision_validation_environment=fake_provision,
                    ensure_repository_idle=lambda _repo: preflight_env,
                    heartbeat_seconds=0,
                    cleanup_root=Path(temp) / "cleanup",
                )

        self.assertEqual(code, 0)
        self.assertEqual(
            [item.args[0] for item in run.call_args_list],
            [
                ["clawpatch", "status", "--json"],
                ["clawpatch", "report", "--status", "uncertain", "--json"],
            ],
        )
        expected_env = _release_clawpatch_env(
            trusted_host_codex_sandbox_bypass=False,
            child_env_overrides=preflight_env,
        )
        self.assertTrue(all(item.kwargs["env"] == expected_env for item in run.call_args_list))

    def test_target_pythonpath_never_reaches_the_clawpatch_sweep(self):
        sweep_called = False

        @contextmanager
        def fake_provision(repo: Path, *, progress, temporary_root: Path):
            yield {"PYTHONPATH": os.pathsep.join((str(repo / "src"), str(repo)))}

        def fake_sweep(_repo: Path, **_kwargs):
            nonlocal sweep_called
            sweep_called = True
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with tempfile.TemporaryDirectory() as temp:
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    ["--repo", temp, "--fresh"],
                    run_sweep=fake_sweep,
                    provision_validation_environment=fake_provision,
                    ensure_repository_idle=lambda _repo: None,
                    heartbeat_seconds=0,
                    cleanup_root=Path(temp) / "cleanup",
                )

        self.assertEqual(code, 2)
        self.assertFalse(sweep_called)
        self.assertIn("Python import environment", output.getvalue())

    def test_repository_path_stays_canonical_after_preflight_retargets_symlink(self):
        received_paths = []

        @contextmanager
        def fake_provision(repo: Path, *, progress, temporary_root: Path):
            received_paths.append(repo)
            yield {}

        def fake_sweep(repo: Path, **_kwargs):
            received_paths.append(repo)
            return {
                "ok": True,
                "finding_count": 0,
                "open_findings": 0,
                "git_head": "abc123",
            }

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo_a = root / "repository-a"
            repo_b = root / "repository-b"
            repo_a.mkdir()
            repo_b.mkdir()
            repo_link = root / "repository"
            try:
                repo_link.symlink_to(repo_a, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlinks are unavailable: {exc}")

            def retarget_after_preflight(repo: Path):
                received_paths.append(repo)
                repo_link.unlink()
                repo_link.symlink_to(repo_b, target_is_directory=True)

            with redirect_stdout(StringIO()):
                code = main(
                    ["--repo", str(repo_link)],
                    run_sweep=fake_sweep,
                    provision_validation_environment=fake_provision,
                    ensure_repository_idle=retarget_after_preflight,
                    heartbeat_seconds=0,
                    cleanup_root=root / "cleanup",
                )

        self.assertEqual(code, 0)
        self.assertEqual(received_paths, [repo_a, repo_a, repo_a])

    def test_resume_phase_explains_source_clean_planned_attempt(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "resume",
                    "current": 1,
                    "total": "?",
                    "finding_id": "fnd_one",
                }
            ),
            "\n[1/?] RESUME INTERRUPTED PLANNED ATTEMPT — "
            "🧟🔧 PICKING THIS SHIT BACK UP\n"
            "finding: fnd_one\n"
            "source changes: none; returning through ClawPatch next",
        )

    def test_resume_phase_displays_recovered_applied_repair_paths(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "resume",
                    "current": 1,
                    "total": "?",
                    "finding_id": "fnd_one",
                    "owned_paths": ["app.py", "test_app.py"],
                }
            ),
            "\n[1/?] RESUME APPLIED REPAIR — 😤🔧 FOUND THE SAVED FIX\n"
            "finding: fnd_one\n"
            "source changes: app.py, test_app.py",
        )

    def test_init_phase_displays_the_exact_command(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "init",
                    "current": "?",
                    "total": "?",
                    "command": "clawpatch init --json",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            ),
            "\n[?/?] INIT (attempt 1/1)\n$ clawpatch init --json",
        )

    def test_trusted_host_revalidation_phase_is_visible(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "revalidate-host",
                    "current": 4,
                    "total": 119,
                    "command": "clawpatch revalidate --finding fnd_one --json",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            ),
            "\n[4/119] REVALIDATE TRUSTED HOST (attempt 1/1) — "
            "🧪💻 CHECK IT OUTSIDE THE SANDBOX BULLSHIT\n"
            "$ clawpatch revalidate --finding fnd_one --json",
        )

    def test_fixed_point_rescan_phase_is_visible(self):
        self.assertEqual(
            _render_event(
                {
                    "phase": "fixed-point-rescan",
                    "current": 14,
                    "total": 14,
                    "command": "start fresh ClawPatch map and complete review",
                    "attempt": 2,
                }
            ),
            "\n[14/14] FRESH FIXED-POINT REVIEW (generation 2) — "
            "🕵️🗑️ CHECKING FOR MORE GARBAGE\n"
            "$ start fresh ClawPatch map and complete review",
        )

    def test_package_installs_external_supervisor_command(self):
        project = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(
            project["scripts"]["clawpatch-supervise"],
            "clawpatch_supervise.clawpatch_external:main",
        )

    def test_terminal_command_shows_one_finding_scoped_fix_and_verified_commit(self):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            progress = kwargs["progress"]
            progress(
                {
                    "phase": "baseline-validation",
                    "current": "?",
                    "total": "?",
                    "command": "configured Manageroo gates",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
            progress(
                {
                    "phase": "map",
                    "current": "?",
                    "total": "?",
                    "command": "clawpatch map --json",
                    "attempt": 1,
                    "max_attempts": 1,
                }
            )
            progress(
                {
                    "phase": "finding",
                    "current": 1,
                    "total": 88,
                    "finding_id": "fnd_one",
                    "command": "clawpatch show --finding fnd_one",
                    "inspection": {
                        "finding": {
                            "id": "fnd_one",
                            "title": "Broken rollback",
                            "severity": "high",
                            "category": "data-loss",
                            "recommendation": "Track publication state.",
                            "reproduction": "Fail the first rename.",
                            "minimumFixScope": "Fix rollback and add a test.",
                            "evidence": [{"path": "release.py", "startLine": 10, "endLine": 20}],
                        },
                        "validation": ["pytest"],
                    },
                }
            )
            progress(
                {
                    "phase": "fix",
                    "current": 1,
                    "total": 88,
                    "finding_id": "fnd_one",
                    "attempt": 1,
                    "max_attempts": 1,
                    "command": "clawpatch fix --finding fnd_one",
                }
            )
            progress(
                {
                    "phase": "fixed",
                    "current": 1,
                    "total": 88,
                    "finding_id": "fnd_one",
                    "commit": "abc123",
                }
            )
            return {
                "ok": True,
                "finding_count": 1,
                "open_findings": 0,
                "uncertain_findings": 0,
                "git_head": "abc123",
                "review_generations": [{"clean": False}, {"clean": True}],
            }

        output = StringIO()
        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(output),
        ):
            code = main(
                ["--fresh"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("[?/?] BASELINE VALIDATION (attempt 1/1)", rendered)
        self.assertIn("$ configured Manageroo gates", rendered)
        self.assertIn("[?/?] MAP (attempt 1/1)", rendered)
        self.assertIn("$ clawpatch map --json", rendered)
        self.assertIn("[1/88] SHOW", rendered)
        self.assertIn("clawpatch show --finding fnd_one", rendered)
        self.assertIn("Broken rollback", rendered)
        self.assertIn("release.py:10-20", rendered)
        self.assertIn("[1/88] FIX", rendered)
        self.assertIn("clawpatch fix --finding fnd_one", rendered)
        self.assertNotIn("RETRY", rendered)
        self.assertNotIn("attempt 2", rendered)
        self.assertIn("[1/88] FIXED — 🔥🔨 FUCK YES, THIS SHIT'S FIXED", rendered)
        self.assertIn("🤬🦶💥 NEW AND FUCKING IMPROVED", rendered)
        self.assertIn("fresh_review_generations=2", rendered)
        self.assertIn("COMPLETE", rendered)
        self.assertIn("EVERY OPEN FINDING WAS PROCESSED", rendered)
        self.assertNotIn("STOPPED", rendered)
        self.assertNotIn("SWEEP FAILED", rendered)
        self.assertNotIn("QUEUE ISN'T CLEAN", rendered)
        self.assertEqual(calls[0][1]["branch"], "current")
        self.assertEqual(calls[0][1]["push_mode"], "each")
        self.assertEqual(calls[0][1]["integration_mode"], "external")
        self.assertTrue(calls[0][1]["advance_uncertain"])
        self.assertTrue(calls[0][1]["fresh"])

    def test_terminal_command_renders_stopped_state_without_retrying(self):
        def fake_sweep(_repo: Path, **kwargs):
            kwargs["progress"](
                {
                    "phase": "stopped",
                    "current": 1,
                    "total": 24,
                    "finding_id": "fnd_one",
                    "outcome": "fix-validation-failed",
                    "owned_paths": ["app.py"],
                }
            )
            raise SafetyError("one fix failed; no automatic continuation")

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        rendered = output.getvalue()
        self.assertEqual(code, 2)
        self.assertIn(
            "[1/24] STOPPED - fix-validation-failed — 🛑💥🤬 FUCK. THIS SHIT ISN'T SAFE TO ADVANCE",
            rendered,
        )
        self.assertIn("source left in place: app.py", rendered)
        self.assertNotIn("RETRY", rendered)

    def test_failed_sweep_reports_stopped_with_open_findings(self):
        def fake_sweep(_repo: Path, **_kwargs):
            return {
                "ok": False,
                "finding_count": 1,
                "open_findings": 1,
                "git_head": "abc123",
                "review_generations": [{"clean": False}],
            }

        output = StringIO()
        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(output),
        ):
            code = main(
                ["--repo", ".", "--fresh"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        rendered = output.getvalue()
        self.assertEqual(code, 2)
        self.assertIn("STOPPED: fixed=1 open=1", rendered)
        self.assertIn("SWEEP FAILED. QUEUE ISN'T CLEAN", rendered)
        self.assertIn("open=1", rendered)
        self.assertNotIn("COMPLETE", rendered)
        self.assertNotIn("QUEUE'S CLEAN", rendered)

    def test_plain_command_retries_a_transient_stop_and_resumes_automatically(self):
        calls = []

        def fake_sweep(_repo: Path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ClawpatchStop(
                    "provider timed out without source progress",
                    repair_action=RepairAction.STOP_TRANSIENT,
                )
            return {"ok": True, "finding_count": 1, "open_findings": 0, "git_head": "abc"}

        output = StringIO()
        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(output),
        ):
            code = main(
                ["--repo", ".", "--retry-seconds", "0.001"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["fresh"])
        self.assertFalse(calls[1]["fresh"])
        self.assertIn("RETRYING AUTOMATICALLY", output.getvalue())
        self.assertIn("COMPLETE", output.getvalue())

    def test_plain_command_waits_for_an_active_repository_run(self):
        preflight_calls = []

        def fake_preflight(_repo: Path):
            preflight_calls.append(True)
            if len(preflight_calls) == 1:
                raise RepositoryBusyError("another supervisor owns this repository")
            return None

        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            code = main(
                ["--repo", ".", "--retry-seconds", "0.001"],
                run_sweep=lambda _repo, **_kwargs: {
                    "ok": True,
                    "finding_count": 0,
                    "open_findings": 0,
                    "git_head": "abc",
                },
                ensure_repository_idle=fake_preflight,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(len(preflight_calls), 2)
        self.assertIn("WAITING FOR THE ACTIVE RUN", output.getvalue())

    def test_busy_exception_output_escapes_terminal_controls(self):
        calls = []

        def fake_preflight(_repo: Path):
            calls.append(True)
            if len(calls) == 1:
                raise RepositoryBusyError(
                    "busy\x1b[2J\nSTOPPED: forged\r\x9b31m\u202ereversed"
                )
            return None

        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            code = main(
                ["--repo", ".", "--retry-seconds", "0.001"],
                run_sweep=lambda _repo, **_kwargs: {
                    "ok": True,
                    "finding_count": 0,
                    "open_findings": 0,
                    "git_head": "abc",
                },
                ensure_repository_idle=fake_preflight,
                heartbeat_seconds=0,
            )

        rendered = output.getvalue()
        self.assertEqual(code, 0)
        self.assertIn(r"busy\x1b[2J\nSTOPPED: forged\r\x9b31m\u202ereversed", rendered)
        self.assertNotIn("\nSTOPPED: forged", rendered)
        for control in ("\x1b", "\r", "\x9b", "\u202e"):
            self.assertNotIn(control, rendered)

    def test_safety_exception_output_escapes_terminal_controls(self):
        def fake_sweep(_repo: Path, **_kwargs):
            raise SafetyError("unsafe\x07\nCOMPLETE: forged\r\x9b31m\u2066isolated")

        with redirect_stdout(StringIO()) as output:
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        rendered = output.getvalue()
        self.assertEqual(code, 2)
        self.assertIn(r"unsafe\x07\nCOMPLETE: forged\r\x9b31m\u2066isolated", rendered)
        self.assertNotIn("\nCOMPLETE: forged", rendered)
        for control in ("\x07", "\r", "\x9b", "\u2066"):
            self.assertNotIn(control, rendered)

    def test_preserved_state_retry_is_not_mislabeled_as_an_active_run(self):
        calls = []

        def fake_sweep(_repo: Path, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise RepositoryBusyError(
                    "Interrupted Clawpatch release progress no longer owns the exact "
                    "current source paths; waiting without discarding them."
                )
            return {
                "ok": True,
                "finding_count": 0,
                "open_findings": 0,
                "git_head": "abc",
            }

        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            code = main(
                ["--repo", ".", "--retry-seconds", "0.001"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("THIS REPOSITORY'S PRESERVED STATE", output.getvalue())
        self.assertNotIn("WAITING FOR THE ACTIVE RUN", output.getvalue())

    def test_plain_command_retries_a_transient_source_clean_command(self):
        calls = []

        def fake_sweep(_repo: Path, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ClawpatchCommandFailure(
                    "review provider timed out",
                    failure=classify_clawpatch_failure("review", 124),
                )
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(StringIO()) as output,
        ):
            code = main(
                ["--repo", ".", "--retry-seconds", "0.001"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("RETRYING AUTOMATICALLY", output.getvalue())

    def test_keyboard_interrupt_warns_that_applied_changes_may_remain(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repository"
            repo.mkdir()
            source = repo / "app.py"
            source.write_text("before\n", encoding="utf-8")

            def fake_sweep(_repo: Path, **_kwargs):
                source.write_text("after\n", encoding="utf-8")
                raise KeyboardInterrupt

            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    ["--repo", str(repo)],
                    run_sweep=fake_sweep,
                    ensure_repository_idle=lambda _repo: None,
                    heartbeat_seconds=0,
                    cleanup_root=Path(temp) / "cleanup",
                )
            final_source = source.read_text(encoding="utf-8")

        rendered = output.getvalue()
        self.assertEqual(code, 130)
        self.assertEqual(final_source, "after\n")
        self.assertIn("applied source or checkpoint changes may remain", rendered)
        self.assertIn("Inspect the repository and ClawPatch state", rendered)
        self.assertNotIn("no source got yeeted", rendered)
        self.assertNotIn("fresh start", rendered)

    def test_terminal_command_requests_a_fresh_run_and_fifteen_minute_shared_timeout(self):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc123"}

        with (
            patch(
                "clawpatch_supervise.clawpatch_external._clawpatch_state_exists",
                return_value=False,
            ),
            redirect_stdout(StringIO()),
        ):
            code = main(
                ["--repo", ".", "--fresh"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1]["fresh"])
        self.assertEqual(calls[0][1]["child_timeout_seconds"], 900)

    @patch("clawpatch_supervise.clawpatch_external._source_paths", return_value=[])
    @patch(
        "clawpatch_supervise.clawpatch_external._existing_queue_is_clean",
        side_effect=AssertionError("explicit --fresh must reset instead of inspecting queue contents"),
    )
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=True)
    def test_explicit_fresh_resets_an_existing_open_queue(
        self, _state_exists, _queue_is_clean, _source_paths
    ):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {
                "ok": True,
                "finding_count": 0,
                "open_findings": 0,
                "git_head": "abc123",
            }

        with redirect_stdout(StringIO()):
            code = main(
                ["--repo", ".", "--fresh"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1]["fresh"])

    @patch("clawpatch_supervise.clawpatch_external._source_paths", return_value=["app.py"])
    @patch("clawpatch_supervise.clawpatch_external._existing_queue_is_clean", return_value=True)
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=True)
    def test_explicit_fresh_passes_retained_source_to_baseline_aware_sweep(
        self, _state_exists, _queue_is_clean, _source_paths
    ):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with redirect_stdout(StringIO()):
            code = main(
                ["--repo", ".", "--fresh"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1]["fresh"])
        self.assertFalse(calls[0][1]["wait_on_preserved_source"])

    @patch("clawpatch_supervise.clawpatch_external._source_paths", return_value=[])
    @patch("clawpatch_supervise.clawpatch_external._existing_queue_is_clean", return_value=True)
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=True)
    def test_explicit_fresh_resets_a_proven_clean_queue(
        self, _state_exists, _queue_is_clean, _source_paths
    ):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc123"}

        with redirect_stdout(StringIO()):
            code = main(
                ["--repo", ".", "--fresh"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1]["fresh"])

    def test_resume_stopped_disables_only_the_default_fresh_start(self):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 1, "open_findings": 0, "git_head": "abc123"}

        with redirect_stdout(StringIO()):
            code = main(
                ["--repo", ".", "--resume-stopped"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertFalse(calls[0][1]["fresh"])
        self.assertEqual(calls[0][1]["integration_mode"], "external")

    @patch("clawpatch_supervise.clawpatch_external._existing_queue_is_clean")
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=True)
    def test_default_run_preserves_existing_open_queue_without_prompting(
        self, _state_exists, queue_is_clean
    ):
        calls = []
        queue_is_clean.return_value = False

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 4, "open_findings": 0, "git_head": "abc"}

        with (
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
            redirect_stdout(StringIO()),
        ):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertFalse(calls[0][1]["fresh"])

    @patch("clawpatch_supervise.clawpatch_external._source_paths", return_value=[])
    @patch("clawpatch_supervise.clawpatch_external._existing_queue_is_clean", return_value=True)
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=True)
    def test_default_run_starts_fresh_without_prompting_at_a_clean_completed_queue(
        self, _state_exists, _queue_is_clean, _source
    ):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with (
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
            redirect_stdout(StringIO()),
        ):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1]["fresh"])

    @patch("clawpatch_supervise.clawpatch_external._source_paths", return_value=["app.py"])
    @patch("clawpatch_supervise.clawpatch_external._existing_queue_is_clean", return_value=True)
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=True)
    def test_clean_queue_with_dirty_source_starts_baseline_aware_fresh_review(
        self, _state_exists, _queue_is_clean, _source
    ):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with (
            patch("builtins.input", side_effect=AssertionError("must not prompt")),
            redirect_stdout(StringIO()),
        ):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1]["fresh"])
        self.assertFalse(calls[0][1]["wait_on_preserved_source"])

    @patch("clawpatch_supervise.clawpatch_external._source_paths", return_value=["app.py"])
    @patch("clawpatch_supervise.clawpatch_external._clawpatch_state_exists", return_value=False)
    def test_default_run_without_queue_uses_baseline_aware_fresh_review(
        self, _state_exists, _source
    ):
        calls = []

        def fake_sweep(repo: Path, **kwargs):
            calls.append((repo, kwargs))
            return {"ok": True, "finding_count": 0, "open_findings": 0, "git_head": "abc"}

        with redirect_stdout(StringIO()):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertTrue(calls[0][1]["fresh"])
        self.assertFalse(calls[0][1]["wait_on_preserved_source"])


if __name__ == "__main__":
    unittest.main()
