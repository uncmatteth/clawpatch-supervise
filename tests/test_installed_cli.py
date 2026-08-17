from __future__ import annotations

import json
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
NETWORK_WHEEL_TESTS_ENV = "CLAWPATCH_SUPERVISE_NETWORK_TESTS"
NETWORK_WHEEL_TESTS_ENABLED = os.environ.get(NETWORK_WHEEL_TESTS_ENV) == "1"


class InstalledConsoleScriptTests(unittest.TestCase):
    def test_build_requirements_are_exactly_pinned_for_the_network_lane(self) -> None:
        manifest = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        requirements = (REPOSITORY_ROOT / "requirements-test.txt").read_text(
            encoding="utf-8"
        ).splitlines()

        self.assertEqual(manifest["build-system"]["requires"], requirements)

    def test_network_lane_wheel_build_uses_isolated_pep517_requirements(self) -> None:
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

    def test_installed_wheel_entrypoint_requires_explicit_network_lane(self) -> None:
        test = type(self).test_clawpatch_supervise_entrypoint_from_installed_wheel

        self.assertEqual(
            getattr(test, "__unittest_skip__", False),
            not NETWORK_WHEEL_TESTS_ENABLED,
        )

    @unittest.skipUnless(
        NETWORK_WHEEL_TESTS_ENABLED,
        f"set {NETWORK_WHEEL_TESTS_ENV}=1 to run the package-index integration lane",
    )
    def test_clawpatch_supervise_entrypoint_from_installed_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = self._build_wheel(root)
            environment = os.environ.copy()
            environment.pop("PYTHONHOME", None)
            environment.pop("PYTHONPATH", None)
            environment["PYTHONNOUSERSITE"] = "1"

            home = root / "home"
            runtime = root / "runtime"
            state = root / "state"
            temporary = root / "tmp"
            local_app_data = root / "local-app-data"
            tools = root / "tools"
            for directory in (home, runtime, state, temporary, local_app_data, tools):
                directory.mkdir(mode=0o700)
            environment.update(
                {
                    "HOME": str(home),
                    "LOCALAPPDATA": str(local_app_data),
                    "TEMP": str(temporary),
                    "TMP": str(temporary),
                    "TMPDIR": str(temporary),
                    "USERPROFILE": str(home),
                    "XDG_RUNTIME_DIR": str(runtime),
                    "XDG_STATE_HOME": str(state),
                }
            )

            if os.name == "nt":
                clawpatch = tools / "clawpatch.cmd"
                clawpatch.write_text(
                    "@echo off\r\n"
                    'if "%~1"=="--version" (echo clawpatch 0.7.2& exit /b 0)\r\n'
                    'if "%~1"=="doctor" if "%~2"=="--json" '
                    '(echo {"state":"missing","provider":"test","providerVersion":"stub"}'
                    "& exit /b 0)\r\n"
                    "exit /b 9\r\n",
                    encoding="ascii",
                )
            else:
                clawpatch = tools / "clawpatch"
                clawpatch.write_text(
                    "#!/bin/sh\n"
                    'if [ "$1" = "--version" ]; then\n'
                    "  printf '%s\\n' 'clawpatch 0.7.2'\n"
                    'elif [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then\n'
                    "  printf '%s\\n' "
                    "'{\"state\":\"missing\",\"provider\":\"test\","
                    "\"providerVersion\":\"stub\"}'\n"
                    "else\n"
                    "  exit 9\n"
                    "fi\n",
                    encoding="ascii",
                )
                clawpatch.chmod(0o700)
            environment["PATH"] = str(tools) + os.pathsep + environment.get("PATH", "")

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

            metadata = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "from importlib.metadata import distribution; "
                    "package = distribution('clawpatch-supervise'); "
                    "entry_point = next(item for item in package.entry_points "
                    "if item.group == 'console_scripts' "
                    "and item.name == 'clawpatch-supervise'); "
                    "print(package.version); print(entry_point.value)",
                ],
                capture_output=True,
                check=False,
                cwd=root,
                env=environment,
                text=True,
                timeout=30,
            )
            self.assertEqual(metadata.returncode, 0, metadata.stderr)
            self.assertEqual(
                metadata.stdout.splitlines(),
                [__version__, "clawpatch_supervise.clawpatch_external:main"],
            )

            imported = subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import clawpatch_supervise; print(clawpatch_supervise.__version__)",
                ],
                capture_output=True,
                check=False,
                cwd=root,
                env=environment,
                text=True,
                timeout=30,
            )
            self.assertEqual(imported.returncode, 0, imported.stderr)
            self.assertEqual(imported.stdout.strip(), __version__)

            def invoke(*arguments: str, cwd: Path = root) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [str(command), *arguments],
                    capture_output=True,
                    check=False,
                    cwd=cwd,
                    env=environment,
                    text=True,
                    timeout=30,
                )

            version = invoke("--version")
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertEqual(version.stdout, f"clawpatch-supervise {__version__}\n")
            self.assertEqual(version.stderr, "")

            help_result = invoke("--help")
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            self.assertIn("usage: clawpatch-supervise", help_result.stdout)
            self.assertIn("--print-state-path", help_result.stdout)

            cleanup = invoke("cleanup", "--dry-run")
            self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
            self.assertIn("ClawPatch Supervise cleanup root:", cleanup.stdout)
            self.assertIn("COMPLETE: inspected=0 removed=0 removed_bytes=0", cleanup.stdout)

            repository = root / "repository"
            repository.mkdir()
            initialized = subprocess.run(
                ["git", "init", "--quiet"],
                capture_output=True,
                check=False,
                cwd=repository,
                env=environment,
                text=True,
                timeout=30,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            state_path = invoke("--repo", str(repository), "--print-state-path")
            self.assertEqual(state_path.returncode, 0, state_path.stderr)
            self.assertTrue(Path(state_path.stdout.strip()).is_relative_to(root))

            doctor = invoke("doctor", "--repo", str(repository))
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            doctor_report = json.loads(doctor.stdout)
            self.assertTrue(doctor_report["ready"])
            self.assertEqual(doctor_report["provider"], "test")
            self.assertEqual(doctor_report["providerVersion"], "stub")
            self.assertEqual(doctor_report["clawpatch"], "clawpatch 0.7.2")

            for arguments, message in (
                (("--timeout-minutes", "0"), "--timeout-minutes must be at least 1"),
                (("--retry-seconds", "nan"), "--retry-seconds must be a finite positive number"),
            ):
                with self.subTest(arguments=arguments):
                    invalid_numeric = invoke(*arguments)
                    self.assertEqual(invalid_numeric.returncode, 2)
                    self.assertIn(message, invalid_numeric.stderr)

            invalid = invoke("--not-a-real-option")
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
