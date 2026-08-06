import os
from pathlib import Path
import tempfile
import unittest

from clawpatch_supervise.runner import CommandRunner


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
