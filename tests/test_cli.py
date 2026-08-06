from __future__ import annotations

from contextlib import contextmanager
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from clawpatch_supervise.clawpatch_external import _render_event, main
from clawpatch_supervise.clawpatch_protocol import RepairAction
from clawpatch_supervise.clawpatch_release import ClawpatchCommandFailure, ClawpatchStop
from clawpatch_supervise.clawpatch_protocol import classify_clawpatch_failure
from clawpatch_supervise.errors import SafetyError


class ExternalClawpatchSupervisorTests(unittest.TestCase):
    def test_version_is_available_without_running_clawpatch(self):
        output = StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(output):
            main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "clawpatch-supervise 0.1.1")

    def test_print_state_path_is_read_only_and_skips_preflight(self):
        repo = Path("/tmp/example-repository")
        expected = Path("/tmp/example-state")
        output = StringIO()
        with patch(
            "clawpatch_supervise.clawpatch_external.external_state_root",
            return_value=expected,
        ) as state_root, redirect_stdout(output):
            result = main(["--repo", str(repo), "--print-state-path"])

        self.assertEqual(result, 0)
        state_root.assert_called_once_with(repo)
        self.assertEqual(output.getvalue().strip(), str(expected))

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
        def fake_provision(repo: Path, *, progress):
            lifecycle.append(("start", repo))
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

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                ["--repo", "."],
                run_sweep=fake_sweep,
                provision_validation_environment=fake_provision,
                ensure_repository_idle=fake_idle,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 0)
        self.assertEqual([item[0] for item in lifecycle], ["idle", "start", "cleanup"])
        self.assertEqual(
            calls[0][1]["child_env_overrides"],
            {
                "TEST_DATABASE_URL": "postgresql://127.0.0.1:49152/test",
                "BTT_ALLOW_DATABASE_RESET": "true",
            },
        )
        self.assertIn("VALIDATION SERVICE START", output.getvalue())
        self.assertLess(
            output.getvalue().index("PROCESS PREFLIGHT"),
            output.getvalue().index("VALIDATION SERVICE START"),
        )
        self.assertEqual(output.getvalue().count("PROCESS PREFLIGHT"), 1)

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
            "\n[1/?] RESUME INTERRUPTED PLANNED ATTEMPT\n"
            "finding: fnd_one\n"
            "source changes: none; returning through ClawPatch next",
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
            "\n[4/119] REVALIDATE TRUSTED HOST (attempt 1/1)\n"
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
            "\n[14/14] FRESH FIXED-POINT REVIEW (generation 2)\n"
            "$ start fresh ClawPatch map and complete review",
        )

    def test_package_installs_external_supervisor_command(self):
        project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
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
                "git_head": "abc123",
                "review_generations": [{"clean": False}, {"clean": True}],
            }

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [],
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
        self.assertIn("[1/88] FIXED", rendered)
        self.assertIn("fresh_review_generations=2", rendered)
        self.assertEqual(calls[0][1]["branch"], "current")
        self.assertEqual(calls[0][1]["push_mode"], "each")
        self.assertEqual(calls[0][1]["integration_mode"], "external")
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
        self.assertIn("[1/24] STOPPED - fix-validation-failed", rendered)
        self.assertIn("source left in place: app.py", rendered)
        self.assertNotIn("RETRY", rendered)

    def test_transient_stop_has_a_distinct_service_retry_exit(self):
        def fake_sweep(_repo: Path, **_kwargs):
            raise ClawpatchStop(
                "provider timed out without source progress",
                repair_action=RepairAction.STOP_TRANSIENT,
            )

        output = StringIO()
        with redirect_stdout(output):
            code = main(
                ["--repo", ".", "--resume-stopped"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 75)
        self.assertIn("TRANSIENT", output.getvalue())
        self.assertIn("--resume-stopped", output.getvalue())

    def test_transient_source_clean_command_failure_uses_the_same_retry_exit(self):
        def fake_sweep(_repo: Path, **_kwargs):
            raise ClawpatchCommandFailure(
                "review provider timed out",
                failure=classify_clawpatch_failure("review", 124),
            )

        with redirect_stdout(StringIO()):
            code = main(
                ["--repo", ".", "--resume-stopped"],
                run_sweep=fake_sweep,
                ensure_repository_idle=lambda _repo: None,
                heartbeat_seconds=0,
            )

        self.assertEqual(code, 75)

    def test_terminal_command_requests_a_fresh_run_and_fifteen_minute_shared_timeout(self):
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
        self.assertEqual(calls[0][1]["child_timeout_seconds"], 900)

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


if __name__ == "__main__":
    unittest.main()
