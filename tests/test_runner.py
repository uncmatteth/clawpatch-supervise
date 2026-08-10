import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from clawpatch_supervise.clawpatch_release import _release_clawpatch_env
from clawpatch_supervise.errors import SafetyError
from clawpatch_supervise.runner import CommandRunner, _terminate_process_group


class CommandRunnerEnvironmentTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://production.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "true",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "NPM_TOKEN": "registry-secret",
            "SSH_AUTH_SOCK": "/tmp/host-agent.sock",
            "CLAWPATCH_TEST_AMBIENT_SENTINEL": "must-not-be-inherited",
        },
    )
    def test_process_group_uses_exact_sanitized_release_environment(self) -> None:
        child_env = _release_clawpatch_env(trusted_host_codex_sandbox_bypass=False)

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
        self.assertNotIn("CLAWPATCH_TEST_AMBIENT_SENTINEL", child_env)
        self.assertEqual(json.loads(result.stdout), child_env)


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
