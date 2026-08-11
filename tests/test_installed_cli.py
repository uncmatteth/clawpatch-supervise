from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from clawpatch_supervise import __version__


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NETWORK_TEST_ENV = "CLAWPATCH_SUPERVISE_RUN_NETWORK_TESTS"


class InstalledConsoleScriptTests(unittest.TestCase):
    def test_build_requirements_are_exactly_pinned_for_the_network_lane(self) -> None:
        manifest = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        requirements = (REPOSITORY_ROOT / "requirements-test.txt").read_text(
            encoding="utf-8"
        ).splitlines()

        self.assertEqual(manifest["build-system"]["requires"], requirements)

    def test_wheel_build_uses_isolated_pep517_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)

            def build(argv, **_kwargs):
                (destination / "clawpatch_supervise-test.whl").touch()
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.object(subprocess, "run", side_effect=build) as run:
                self._build_wheel(destination)

            argv = run.call_args.args[0]
            self.assertIn("--use-pep517", argv)
            self.assertNotIn("--no-build-isolation", argv)
            self.assertNotIn("--no-index", argv)
            self.assertNotIn("PIP_NO_INDEX", run.call_args.kwargs["env"])
            self.assertNotIn("PIP_NO_BUILD_ISOLATION", run.call_args.kwargs["env"])

    @unittest.skipUnless(
        os.environ.get(NETWORK_TEST_ENV) == "1",
        f"network integration test; set {NETWORK_TEST_ENV}=1 to run",
    )
    def test_wheel_installs_console_script_and_propagates_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = self._build_wheel(root)
            environment = os.environ.copy()
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
            environment["PYTHONNOUSERSITE"] = "1"

            virtual_environment = root / "venv"
            venv.EnvBuilder(with_pip=True).create(virtual_environment)
            bin_directory = virtual_environment / ("Scripts" if os.name == "nt" else "bin")
            python = bin_directory / ("python.exe" if os.name == "nt" else "python")
            command = bin_directory / (
                "clawpatch-supervise.exe" if os.name == "nt" else "clawpatch-supervise"
            )
            installed = subprocess.run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-deps",
                    "--no-index",
                    str(wheel),
                ],
                capture_output=True,
                check=False,
                cwd=root,
                env=environment,
                text=True,
                timeout=120,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            version = subprocess.run(
                [str(command), "--version"],
                capture_output=True,
                check=False,
                cwd=root,
                env=environment,
                text=True,
                timeout=30,
            )
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout.strip(), f"clawpatch-supervise {__version__}")

            invalid = subprocess.run(
                [str(command), "--not-a-real-option"],
                capture_output=True,
                check=False,
                cwd=root,
                env=environment,
                text=True,
                timeout=30,
            )
            self.assertEqual(invalid.returncode, 2, invalid.stderr)
            self.assertIn("unrecognized arguments: --not-a-real-option", invalid.stderr)

    @staticmethod
    def _build_wheel(destination: Path) -> Path:
        source = destination / "source"
        source.mkdir()
        for filename in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(REPOSITORY_ROOT / filename, source / filename)
        shutil.copytree(REPOSITORY_ROOT / "src", source / "src")
        environment = os.environ.copy()
        environment.pop("PIP_NO_INDEX", None)
        environment.pop("PIP_NO_BUILD_ISOLATION", None)
        built = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--use-pep517",
                "--no-deps",
                "--wheel-dir",
                str(destination),
                str(source),
            ],
            capture_output=True,
            check=False,
            cwd=destination,
            env=environment,
            text=True,
            timeout=120,
        )
        if built.returncode != 0:
            raise AssertionError(built.stderr)
        wheels = list(destination.glob("clawpatch_supervise-*.whl"))
        if len(wheels) != 1:
            raise AssertionError(f"Expected one built wheel, found: {wheels}")
        return wheels[0]


if __name__ == "__main__":
    unittest.main()
