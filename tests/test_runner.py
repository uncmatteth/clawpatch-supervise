import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from clawpatch_supervise.clawpatch_release import _release_clawpatch_env
from clawpatch_supervise.runner import CommandRunner


class CommandRunnerEnvironmentTests(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "DATABASE_URL": "postgresql://production.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "true",
        },
    )
    def test_process_group_uses_exact_sanitized_release_environment(self) -> None:
        child_env = _release_clawpatch_env(trusted_host_codex_sandbox_bypass=False)

        result = CommandRunner().run(
            [
                sys.executable,
                "-c",
                (
                    "import json, os; "
                    "print(json.dumps({name: name in os.environ for name in "
                    "('DATABASE_URL', 'BTT_ALLOW_DATABASE_RESET')}))"
                ),
            ],
            cwd=Path.cwd(),
            timeout_seconds=30,
            env=child_env,
            kill_process_group=True,
        )

        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {"DATABASE_URL": False, "BTT_ALLOW_DATABASE_RESET": False},
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
