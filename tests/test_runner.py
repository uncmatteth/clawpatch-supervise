import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import call, patch

from _process_tree_test_support import assert_blocked_descendant_exited
from clawpatch_supervise.clawpatch_protocol import (
    ClawpatchFailureKind,
    RepairAction,
    decide_repair_transition,
)
from clawpatch_supervise.clawpatch_release import (
    ClawpatchCommandFailure,
    _must_clawpatch,
    _release_clawpatch_env,
    _run,
)
from clawpatch_supervise.errors import SafetyError
from clawpatch_supervise.runner import CommandRunner, _terminate_process_group


class CommandRunnerEnvironmentTests(unittest.TestCase):
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
    def test_process_group_uses_exact_sanitized_release_environment(self) -> None:
        child_env = _release_clawpatch_env(
            trusted_host_codex_sandbox_bypass=False,
            child_env_overrides={
                "CLAWPATCH_TEST_VALIDATION_OVERRIDE": "owned-validation-override"
            },
        )

        result = CommandRunner().run(
            [
                sys.executable,
                "-c",
                "import json, os; print(json.dumps(dict(os.environ), sort_keys=True))",
            ],
            cwd=Path.cwd(),
            timeout_seconds=30,
            env=child_env,
            kill_process_group=True,
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        observed_env = json.loads(result.stdout)
        sensitive_environment = {
            "DATABASE_URL": "postgresql://production.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "ambient-reset-enabled",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "NPM_TOKEN": "registry-secret",
            "SSH_AUTH_SOCK": "/tmp/host-agent.sock",
        }
        for name, value in sensitive_environment.items():
            self.assertNotIn(name, child_env)
            self.assertNotIn(name, observed_env)
            self.assertNotIn(value, json.dumps(child_env))
            self.assertNotIn(value, result.stdout)
        self.assertNotIn("CLAWPATCH_TEST_AMBIENT_SENTINEL", child_env)
        self.assertNotIn("CLAWPATCH_TEST_AMBIENT_SENTINEL", observed_env)
        self.assertEqual(
            observed_env["CLAWPATCH_TEST_VALIDATION_OVERRIDE"],
            "owned-validation-override",
        )
        self.assertEqual(observed_env.get("PATH"), os.environ.get("PATH"))
        self.assertEqual(observed_env, child_env)


class CommandRunnerLoggingTests(unittest.TestCase):
    def test_log_name_must_be_a_nonempty_single_filename_component(self) -> None:
        invalid_names = (
            "",
            ".",
            "..",
            "../escape",
            "parent/escape",
            r"parent\escape",
            "/tmp/escape",
            r"C:\escape",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner = CommandRunner(log_root=root / "logs")

            for log_name in invalid_names:
                with self.subTest(log_name=log_name):
                    with self.assertRaisesRegex(
                        SafetyError,
                        "single filename components",
                    ):
                        runner.run(
                            [sys.executable, "-c", "raise SystemExit(0)"],
                            cwd=root,
                            log_name=log_name,
                        )

            self.assertEqual(list((root / "logs").iterdir()), [])

    def test_valid_log_name_writes_one_json_file_beneath_log_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_root = root / "logs"

            result = CommandRunner(log_root=log_root).run(
                [sys.executable, "-c", "print('logged')"],
                cwd=root,
                log_name="valid-slug",
            )

            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual([path.name for path in log_root.iterdir()], ["valid-slug.json"])
            payload = json.loads((log_root / "valid-slug.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["stdout"].strip(), "logged")

    def test_credential_urls_are_redacted_from_results_and_logs(self) -> None:
        userinfo_secret = "userinfo-secret"
        query_secret = "query-secret"
        stdout_url = f"https://alice:{userinfo_secret}@example.invalid/repository"
        stderr_url = (
            f"https://example.invalid/repository?token={query_secret}&mode=read"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log_root = root / "logs"

            result = CommandRunner(log_root=log_root).run(
                [
                    sys.executable,
                    "-c",
                    "import sys; print(sys.argv[1]); print(sys.argv[2], file=sys.stderr)",
                    stdout_url,
                    stderr_url,
                ],
                cwd=root,
                log_name="credential-urls",
            )

            payload = json.loads(
                (log_root / "credential-urls.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertIn("https://<REDACTED>@example.invalid/repository", result.stdout)
            self.assertIn(
                "https://example.invalid/repository?token=<REDACTED>&mode=read",
                result.stderr,
            )
            for serialized in (json.dumps(result.to_dict()), json.dumps(payload)):
                self.assertNotIn(userinfo_secret, serialized)
                self.assertNotIn(query_secret, serialized)
                self.assertIn("example.invalid/repository", serialized)


class WindowsProcessTreeTerminationTests(unittest.TestCase):
    @patch("clawpatch_supervise.runner.os.name", "nt")
    def test_failed_taskkill_raises_after_killing_parent(self) -> None:
        class Process:
            pid = 4321
            stdin = None
            stdout = None
            stderr = None
            killed = False

            def kill(self) -> None:
                self.killed = True

            def communicate(self, *, timeout: int) -> tuple[str, str]:
                if timeout != 5:
                    raise AssertionError(f"unexpected cleanup timeout: {timeout}")
                return "", ""

        process = Process()
        failed = subprocess.CompletedProcess([], 1)

        with (
            patch("clawpatch_supervise.runner.subprocess.run", return_value=failed),
            self.assertRaisesRegex(SafetyError, "process-tree termination could not be proven"),
        ):
            _terminate_process_group(process)

        self.assertTrue(process.killed)


@unittest.skipUnless(os.name == "posix", "POSIX process-group behavior")
class PosixProcessTreeTerminationTests(unittest.TestCase):
    def test_signal_terminated_clawpatch_command_becomes_controlled_terminal_failure(
        self,
    ) -> None:
        result = _run(
            [
                sys.executable,
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            ],
            cwd=Path.cwd(),
            timeout=30,
        )

        self.assertEqual(result.returncode, -signal.SIGTERM)
        with (
            patch(
                "clawpatch_supervise.clawpatch_release._run_clawpatch",
                return_value=result,
            ),
            self.assertRaises(ClawpatchCommandFailure) as raised,
        ):
            _must_clawpatch(
                Path.cwd(),
                ["clawpatch", "review"],
                env={},
                timeout=30,
            )

        failure = raised.exception.failure
        self.assertEqual(failure.exit_code, -signal.SIGTERM)
        self.assertEqual(failure.kind, ClawpatchFailureKind.COMMAND_FAILED)
        self.assertEqual(
            decide_repair_transition(failure=failure).action,
            RepairAction.STOP_TERMINAL,
        )

    @patch("clawpatch_supervise.runner.os.name", "posix")
    def test_surviving_group_is_killed_when_parent_communicate_completes(self) -> None:
        class Process:
            pid = 4321
            stdin = None
            stdout = None
            stderr = None

            def communicate(self, *, timeout: int) -> tuple[str, str]:
                return "parent output", ""

        process = Process()

        with (
            patch(
                "clawpatch_supervise.runner._posix_process_group_exists",
                return_value=True,
            ),
            patch(
                "clawpatch_supervise.runner._wait_for_posix_process_group_exit",
                return_value=True,
            ) as wait_for_exit,
            patch("clawpatch_supervise.runner.os.killpg") as killpg,
        ):
            stdout, stderr = _terminate_process_group(process)

        self.assertEqual((stdout, stderr), ("parent output", ""))
        self.assertEqual(
            killpg.call_args_list,
            [
                call(4321, signal.SIGTERM),
                call(4321, signal.SIGKILL),
            ],
        )
        wait_for_exit.assert_called_once_with(4321)

    @patch("clawpatch_supervise.runner.os.name", "posix")
    def test_unproven_exit_after_sigkill_raises_safety_error(self) -> None:
        class Process:
            pid = 4321
            stdin = None
            stdout = None
            stderr = None

            def communicate(self, *, timeout: int) -> tuple[str, str]:
                raise subprocess.TimeoutExpired(["child"], timeout)

            def wait(self, *, timeout: int) -> int:
                raise subprocess.TimeoutExpired(["child"], timeout)

            def poll(self) -> None:
                return None

        process = Process()

        with (
            patch(
                "clawpatch_supervise.runner._wait_for_posix_process_group_exit",
                return_value=False,
            ),
            patch("clawpatch_supervise.runner.os.killpg") as killpg,
            self.assertRaisesRegex(
                SafetyError,
                r"termination could not be proven.*process group 4321",
            ),
        ):
            _terminate_process_group(process)

        self.assertEqual(
            killpg.call_args_list,
            [
                call(4321, signal.SIGTERM),
                call(4321, signal.SIGKILL),
            ],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX process-group integration")
    def test_release_run_kills_descendant_after_parent_closes_pipes(self) -> None:
        child_source = (
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w', encoding='utf-8').write(f'{os.getpid()} {os.getpgrp()}\\n')\n"
            "os.close(0); os.close(1); os.close(2)\n"
            "while not os.path.exists(sys.argv[2]): time.sleep(0.01)\n"
            "open(sys.argv[3], 'w', encoding='utf-8').write('escaped\\n')\n"
        )
        parent_source = (
            "import pathlib, subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], "
            "sys.argv[3], sys.argv[4]])\n"
            "while not pathlib.Path(sys.argv[2]).exists(): time.sleep(0.01)\n"
            "time.sleep(30)\n"
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ready = root / "descendant-ready.txt"
            release = root / "descendant-release.txt"
            sentinel = root / "descendant-wrote.txt"

            result = _run(
                [
                    sys.executable,
                    "-c",
                    parent_source,
                    child_source,
                    str(ready),
                    str(release),
                    str(sentinel),
                ],
                cwd=root,
                timeout=1,
            )

            self.assertEqual(result.returncode, 124, result.stdout)
            self.assertIn("TIMEOUT", result.stdout)
            self.assertTrue(ready.is_file())
            assert_blocked_descendant_exited(
                ready=ready,
                release=release,
                sentinel=sentinel,
            )


@unittest.skipUnless(os.name == "nt", "Windows command runner only")
class WindowsCommandRunnerTests(unittest.TestCase):
    def test_batch_file_under_space_path_runs_without_quote_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            command_dir = root / "Program Files" / "example"
            command_dir.mkdir(parents=True)
            command = command_dir / "echo-argument.cmd"
            command.write_text("@echo off\r\necho %~1\r\n", encoding="ascii")

            for kill_process_group in (True, False):
                with self.subTest(kill_process_group=kill_process_group):
                    result = CommandRunner().run(
                        [str(command), "hello world"],
                        cwd=root,
                        timeout_seconds=30,
                        kill_process_group=kill_process_group,
                    )

                    self.assertEqual(result.exit_code, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "hello world")


if __name__ == "__main__":
    unittest.main()
