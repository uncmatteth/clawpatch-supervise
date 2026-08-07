import os
from pathlib import Path
import subprocess
import tempfile
import tomllib
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLAWPATCH_VERSION = "0.7.2"
CLAWHUB_VERSION = "0.19.1"


class InstallerContractTests(unittest.TestCase):
    def test_linux_installer_requires_clawhub_prerequisite_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            (fake_bin / "bash").symlink_to("/bin/bash")

            command_stub = fake_bin / "command-stub"
            command_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command_stub.chmod(0o755)
            (fake_bin / "git").symlink_to(command_stub)
            (fake_bin / "clawpatch").symlink_to(command_stub)

            python = fake_bin / "python3"
            invocation_log = root / "python-invocations.log"
            python.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$CLAWPATCH_TEST_LOG"\n'
                "exit 0\n",
                encoding="utf-8",
            )
            python.chmod(0o755)

            install_root = root / "install"
            bin_dir = root / "installed-bin"
            bin_dir.mkdir()
            previous_command = root / "previous-clawpatch-supervise"
            previous_command.write_text("previous installation\n", encoding="utf-8")
            installed_command = bin_dir / "clawpatch-supervise"
            installed_command.symlink_to(previous_command)
            environment = os.environ.copy()
            environment.update(
                {
                    "CLAWPATCH_SUPERVISE_BIN_DIR": str(bin_dir),
                    "CLAWPATCH_SUPERVISE_HOME": str(install_root),
                    "CLAWPATCH_SUPERVISE_PYTHON": str(python),
                    "CLAWPATCH_TEST_LOG": str(invocation_log),
                    "PATH": str(fake_bin),
                }
            )

            result = subprocess.run(
                [str(REPOSITORY_ROOT / "scripts" / "install.sh")],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("ClawHub is missing and npm is unavailable.", result.stderr)
            self.assertFalse(install_root.exists())
            self.assertEqual(installed_command.resolve(), previous_command)
            self.assertNotIn("-m venv", invocation_log.read_text(encoding="utf-8"))

    def test_linux_installer_passes_exact_versions_to_npm(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            command_stub = root / "command-stub"
            command_stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command_stub.chmod(0o755)

            python = fake_bin / "python3"
            python.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "-c" ]; then exit 0; fi\n'
                'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
                '  mkdir -p "$3/bin"\n'
                '  cp "$CLAWPATCH_TEST_COMMAND_STUB" "$3/bin/python"\n'
                '  cp "$CLAWPATCH_TEST_COMMAND_STUB" "$3/bin/clawpatch-supervise"\n'
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            python.chmod(0o755)

            npm = fake_bin / "npm"
            npm.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$CLAWPATCH_TEST_LOG"\n'
                'if [ "$1" = "prefix" ]; then\n'
                '  printf "%s\\n" "$CLAWPATCH_TEST_NPM_PREFIX"\n'
                "  exit 0\n"
                "fi\n"
                'if [ "$1" = "install" ] && [ "$2" = "--global" ]; then\n'
                '  mkdir -p "$CLAWPATCH_TEST_NPM_PREFIX/bin"\n'
                '  cp "$CLAWPATCH_TEST_COMMAND_STUB" '
                '"$CLAWPATCH_TEST_NPM_PREFIX/bin/clawpatch"\n'
                "  exit 0\n"
                "fi\n"
                "exit 23\n",
                encoding="utf-8",
            )
            npm.chmod(0o755)

            invocation_log = root / "npm-invocations.log"
            install_root = root / "install"
            npm_prefix = root / "npm-prefix"
            environment = os.environ.copy()
            environment.update(
                {
                    "CLAWPATCH_SUPERVISE_BIN_DIR": str(root / "installed-bin"),
                    "CLAWPATCH_SUPERVISE_HOME": str(install_root),
                    "CLAWPATCH_SUPERVISE_PYTHON": str(python),
                    "CLAWPATCH_TEST_COMMAND_STUB": str(command_stub),
                    "CLAWPATCH_TEST_LOG": str(invocation_log),
                    "CLAWPATCH_TEST_NPM_PREFIX": str(npm_prefix),
                    "PATH": f"{fake_bin}:/usr/bin:/bin",
                }
            )

            result = subprocess.run(
                [str(REPOSITORY_ROOT / "scripts" / "install.sh")],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 23)
            self.assertEqual(
                invocation_log.read_text(encoding="utf-8").splitlines(),
                [
                    f"install --global clawpatch@{CLAWPATCH_VERSION}",
                    "prefix --global",
                    (
                        f"install --prefix {install_root}/clawhub --no-fund "
                        f"--no-audit clawhub@{CLAWHUB_VERSION}"
                    ),
                ],
            )

    def test_linux_installer_rejects_python_older_than_3_11_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            python = root / "python3.10"
            invocation_log = root / "python-invocations.log"
            python.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$CLAWPATCH_TEST_LOG"\n'
                'if [ "$1" = "-c" ]; then exit 1; fi\n'
                "exit 0\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            install_root = root / "install"
            environment = os.environ.copy()
            environment.update(
                {
                    "CLAWPATCH_SUPERVISE_HOME": str(install_root),
                    "CLAWPATCH_SUPERVISE_PYTHON": str(python),
                    "CLAWPATCH_TEST_LOG": str(invocation_log),
                }
            )

            result = subprocess.run(
                [str(REPOSITORY_ROOT / "scripts" / "install.sh")],
                capture_output=True,
                check=False,
                env=environment,
                text=True,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("Python 3.11 or newer is required.", result.stderr)
            self.assertFalse(install_root.exists())
            self.assertNotIn("-m venv", invocation_log.read_text(encoding="utf-8"))

    def test_installer_defaults_match_the_packaged_release(self) -> None:
        project = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        version = project["version"]
        linux = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        windows = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        self.assertIn(f'CLAWPATCH_SUPERVISE_VERSION:-{version}', linux)
        self.assertIn(f'[string]$Version = "{version}"', windows)

    def test_linux_installer_bootstraps_pinned_clawpatch_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

        condition = "if ! command -v clawpatch >/dev/null 2>&1; then"
        install = 'npm install --global "clawpatch@${clawpatch_version}"'

        self.assertIn(f'readonly clawpatch_version="{CLAWPATCH_VERSION}"', script)
        self.assertNotIn("@latest", script)
        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn('command -v npm >/dev/null 2>&1', script)
        self.assertGreater(script.rindex("command -v clawpatch"), script.index(install))

    def test_windows_installer_bootstraps_pinned_clawpatch_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        lookup = "$clawpatch = Get-Command clawpatch -ErrorAction SilentlyContinue"
        condition = "if ($null -eq $clawpatch)"
        install = 'install --global "clawpatch@$ClawPatchVersion"'

        self.assertIn(f'$ClawPatchVersion = "{CLAWPATCH_VERSION}"', script)
        self.assertNotIn("@latest", script)
        self.assertIn(lookup, script)
        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn("Get-Command npm -ErrorAction SilentlyContinue", script)
        self.assertGreater(script.rindex("Get-Command clawpatch"), script.index(install))

    def test_linux_installer_bootstraps_pinned_clawhub_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")

        condition = "if command -v clawhub >/dev/null 2>&1; then"
        install = (
            'npm install --prefix "$clawhub_root" --no-fund --no-audit '
            '"clawhub@${clawhub_version}"'
        )

        self.assertIn(f'readonly clawhub_version="{CLAWHUB_VERSION}"', script)
        self.assertNotIn("@latest", script)
        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn('command -v npm >/dev/null 2>&1', script)
        self.assertIn('"$clawhub_command" --cli-version', script)

    def test_windows_installer_bootstraps_pinned_clawhub_only_when_missing(self) -> None:
        script = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        lookup = "$clawHubCommand = Get-Command clawhub.cmd, clawhub.exe, clawhub"
        condition = "if ($null -eq $clawHubCommand)"
        install = (
            'install --prefix $clawHubRoot --no-fund --no-audit '
            '"clawhub@$ClawHubVersion"'
        )

        self.assertIn(f'$ClawHubVersion = "{CLAWHUB_VERSION}"', script)
        self.assertNotIn("@latest", script)
        self.assertIn(lookup, script)
        self.assertIn(condition, script)
        self.assertEqual(script.count(install), 1)
        self.assertLess(script.index(condition), script.index(install))
        self.assertIn("Get-Command npm.cmd, npm.exe, npm", script)
        self.assertIn("& $clawHubPath --cli-version", script)


if __name__ == "__main__":
    unittest.main()
