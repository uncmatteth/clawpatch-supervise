import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLAWPATCH_VERSION = "0.7.2"
CLAWHUB_VERSION = "0.19.1"


class InstallerContractTests(unittest.TestCase):
    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run_linux_installer(
        self,
        *,
        clawpatch_present: bool,
        clawhub_present: bool,
        clawpatch_version: str = CLAWPATCH_VERSION,
        clawhub_version: str = CLAWHUB_VERSION,
        npm_mode: str = "success",
        source_package: Path | str = REPOSITORY_ROOT,
        source_sha256: str | None = None,
        supervisor_version_fails: bool = False,
        node_version: str = "v22.0.0",
        verify_repo: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
        root = Path(self._temporary_directory.name)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        command_stub = root / "command-stub"
        self._write_executable(
            command_stub,
            "#!/bin/sh\n"
            'case "${0##*/}:$1" in\n'
            '  clawpatch:--version) printf "%s\\n" "$CLAWPATCH_TEST_CLAWPATCH_VERSION" ;;\n'
            '  clawhub:--cli-version) printf "%s\\n" "$CLAWPATCH_TEST_CLAWHUB_VERSION" ;;\n'
            '  clawpatch-supervise:--version)\n'
            '    [ "$CLAWPATCH_TEST_SUPERVISOR_VERSION_FAILS" != "true" ] || exit 26\n'
            '    printf "0.1.21\\n" ;;\n'
            '  clawpatch-supervise:doctor)\n'
            '    command -v clawpatch >/dev/null 2>&1 || exit 27\n'
            '    clawpatch --version >/dev/null 2>&1 || exit 28 ;;\n'
            "esac\n"
            "exit 0\n",
        )

        for command in ("bash", "cp", "ln", "mkdir", "mktemp", "mv", "rm", "sed"):
            command_path = shutil.which(command)
            self.assertIsNotNone(command_path)
            (fake_bin / command).symlink_to(command_path)
        (fake_bin / "git").symlink_to(command_stub)
        self._write_executable(
            fake_bin / "node",
            '#!/bin/sh\nprintf "%s\\n" "$CLAWPATCH_TEST_NODE_VERSION"\n',
        )
        installed_command_stub = root / "installed-command-stub"
        self._write_executable(
            installed_command_stub,
            "#!/bin/sh\n"
            'case "${0##*/}:$1" in\n'
            f'  clawpatch:--version) printf "{CLAWPATCH_VERSION}\\n" ;;\n'
            f'  clawhub:--cli-version) printf "{CLAWHUB_VERSION}\\n" ;;\n'
            "esac\n"
            "exit 0\n",
        )
        if clawpatch_present:
            (fake_bin / "clawpatch").symlink_to(command_stub)
        if clawhub_present:
            (fake_bin / "clawhub").symlink_to(command_stub)

        python = fake_bin / "python3"
        self._write_executable(
            python,
            "#!/bin/sh\n"
            'if [ "$1" = "-c" ] || [ "$1" = "-" ]; then\n'
            '  exec "$CLAWPATCH_TEST_REAL_PYTHON" "$@"\n'
            "fi\n"
            'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
            '  mkdir -p "$3/bin"\n'
            '  cp "$CLAWPATCH_TEST_COMMAND_STUB" "$3/bin/python"\n'
            '  cp "$CLAWPATCH_TEST_COMMAND_STUB" "$3/bin/clawpatch-supervise"\n'
            "fi\n"
            "exit 0\n",
        )

        invocation_log = root / "npm-invocations.log"
        npm_prefix = root / "npm-prefix"
        if npm_mode != "missing":
            self._write_executable(
                fake_bin / "npm",
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$CLAWPATCH_TEST_LOG"\n'
                'if [ "$1" = "install" ] && [ "$2" = "--global" ]; then\n'
                '  [ "$CLAWPATCH_TEST_NPM_MODE" != "global-unwritable" ] || exit 23\n'
                '  mkdir -p "$CLAWPATCH_TEST_NPM_PREFIX/bin"\n'
                '  cp "$CLAWPATCH_TEST_COMMAND_STUB" '
                '"$CLAWPATCH_TEST_NPM_PREFIX/bin/clawpatch"\n'
                "  exit 0\n"
                "fi\n"
                'if [ "$1" = "prefix" ]; then\n'
                '  printf "%s\\n" "$CLAWPATCH_TEST_NPM_PREFIX"\n'
                "  exit 0\n"
                "fi\n"
                'if [ "$1" = "install" ] && [ "$2" = "--prefix" ]; then\n'
                '  if [ "${6%%@*}" = "clawpatch" ]; then\n'
                '    [ "$CLAWPATCH_TEST_NPM_MODE" != "fail-clawpatch" ] || exit 23\n'
                '    mkdir -p "$3/node_modules/.bin"\n'
                '    cp "$CLAWPATCH_TEST_INSTALLED_COMMAND_STUB" "$3/node_modules/.bin/clawpatch"\n'
                "    exit 0\n"
                "  fi\n"
                '  [ "$CLAWPATCH_TEST_NPM_MODE" != "fail-clawhub" ] || exit 24\n'
                '  mkdir -p "$3/node_modules/.bin"\n'
                '  cp "$CLAWPATCH_TEST_INSTALLED_COMMAND_STUB" "$3/node_modules/.bin/clawhub"\n'
                "  exit 0\n"
                "fi\n"
                "exit 25\n",
            )

        install_root = root / "install"
        environment = os.environ.copy()
        environment.update(
            {
                "CLAWPATCH_SUPERVISE_BIN_DIR": str(root / "installed-bin"),
                "CLAWPATCH_SUPERVISE_HOME": str(install_root),
                "CLAWPATCH_SUPERVISE_PYTHON": str(python),
                "CLAWPATCH_SUPERVISE_SOURCE": str(source_package),
                "CLAWPATCH_TEST_COMMAND_STUB": str(command_stub),
                "CLAWPATCH_TEST_INSTALLED_COMMAND_STUB": str(installed_command_stub),
                "CLAWPATCH_TEST_CLAWPATCH_VERSION": clawpatch_version,
                "CLAWPATCH_TEST_CLAWHUB_VERSION": clawhub_version,
                "CLAWPATCH_TEST_LOG": str(invocation_log),
                "CLAWPATCH_TEST_NPM_MODE": npm_mode,
                "CLAWPATCH_TEST_NPM_PREFIX": str(npm_prefix),
                "CLAWPATCH_TEST_REAL_PYTHON": sys.executable,
                "CLAWPATCH_TEST_SUPERVISOR_VERSION_FAILS": str(
                    supervisor_version_fails
                ).lower(),
                "CLAWPATCH_TEST_NODE_VERSION": node_version,
                "PATH": str(fake_bin),
            }
        )
        environment.pop("CLAWPATCH_SUPERVISE_SHA256", None)
        if source_sha256 is not None:
            environment["CLAWPATCH_SUPERVISE_SHA256"] = source_sha256
        if verify_repo:
            environment["CLAWPATCH_SUPERVISE_VERIFY_REPO"] = str(root)
        result = subprocess.run(
            [str(REPOSITORY_ROOT / "scripts" / "install.sh")],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        invocations = (
            invocation_log.read_text(encoding="utf-8").splitlines()
            if invocation_log.exists()
            else []
        )
        return result, invocations, install_root

    @staticmethod
    def _write_batch(path: Path, content: str) -> None:
        path.write_text(content.replace("\n", "\r\n"), encoding="ascii")

    def _run_windows_installer(
        self,
        *,
        clawpatch_present: bool,
        clawhub_present: bool,
        clawpatch_version: str = CLAWPATCH_VERSION,
        clawhub_version: str = CLAWHUB_VERSION,
        npm_mode: str = "success",
        python_version_fails: bool = False,
        node_version: str = "v22.0.0",
        source_package: Path | str = REPOSITORY_ROOT,
        source_sha256: str = "",
    ) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
        root = Path(self._temporary_directory.name)
        fake_bin = root / "bin with spaces"
        fake_bin.mkdir()
        command_stub = root / "command-stub.cmd"
        self._write_batch(
            command_stub,
            "@echo off\n"
            'if /I "%~n0"=="clawpatch" if "%1"=="--version" '
            "echo %CLAWPATCH_TEST_CLAWPATCH_VERSION%\n"
            'if /I "%~n0"=="clawhub" if "%1"=="--cli-version" '
            "echo %CLAWPATCH_TEST_CLAWHUB_VERSION%\n"
            "exit /b 0\n",
        )
        if clawpatch_present:
            shutil.copyfile(command_stub, fake_bin / "clawpatch.cmd")
        if clawhub_present:
            shutil.copyfile(command_stub, fake_bin / "clawhub.cmd")
        self._write_batch(
            fake_bin / "node.cmd",
            "@echo off\necho %CLAWPATCH_TEST_NODE_VERSION%\n",
        )
        installed_command_stub = root / "installed-command-stub.cmd"
        self._write_batch(
            installed_command_stub,
            "@echo off\n"
            f'if /I "%~n0"=="clawpatch" if "%1"=="--version" echo {CLAWPATCH_VERSION}\n'
            f'if /I "%~n0"=="clawhub" if "%1"=="--cli-version" echo {CLAWHUB_VERSION}\n'
            "exit /b 0\n",
        )
        venv_python_stub = root / "venv-python.cmd"
        self._write_batch(venv_python_stub, "@echo off\nexit /b 0\n")
        supervisor_stub = root / "clawpatch-supervise.cmd"
        self._write_batch(
            supervisor_stub,
            "@echo off\n"
            'if "%1"=="--version" echo 0.1.27\n'
            'if "%1"=="doctor" where clawpatch.cmd >nul || exit /b 27\n'
            "exit /b 0\n",
        )

        self._write_batch(
            fake_bin / "py.cmd",
            "@echo off\n"
            'if not "%1"=="-3" exit /b 91\n'
            'if "%2"=="--version" (\n'
            '  if /I "%CLAWPATCH_TEST_PYTHON_VERSION_FAILS%"=="true" (\n'
            "    echo Python 3.10.0\n"
            "  ) else (\n"
            "    echo Python 3.12.0\n"
            "  )\n"
            "  exit /b 0\n"
            ")\n"
            'if not "%2"=="-m" exit /b 92\n'
            'if not "%3"=="venv" exit /b 93\n'
            'mkdir "%~4\\Scripts"\n'
            'copy /Y "%CLAWPATCH_TEST_VENV_PYTHON_STUB%" "%~4\\Scripts\\python.cmd" >nul\n'
            'copy /Y "%CLAWPATCH_TEST_SUPERVISOR_STUB%" '
            '"%~4\\Scripts\\clawpatch-supervise.cmd" >nul\n'
            "exit /b 0\n",
        )

        invocation_log = root / "npm-invocations.log"
        npm_prefix = root / "npm-prefix"
        if npm_mode != "missing":
            self._write_batch(
                fake_bin / "npm.cmd",
                "@echo off\n"
                'echo %*>>"%CLAWPATCH_TEST_LOG%"\n'
                'if /I "%CLAWPATCH_TEST_NPM_MODE%"=="fail-clawpatch" '
                'if /I "%1"=="install" if /I "%~6"=="clawpatch@latest" exit /b 23\n'
                'if /I "%1"=="install" if /I "%2"=="--prefix" '
                'if /I "%~6"=="clawpatch@latest" (\n'
                '  mkdir "%~3\\node_modules\\.bin"\n'
                '  copy /Y "%CLAWPATCH_TEST_INSTALLED_COMMAND_STUB%" '
                '"%~3\\node_modules\\.bin\\clawpatch.cmd" >nul\n'
                "  exit /b 0\n"
                ")\n"
                'if /I "%CLAWPATCH_TEST_NPM_MODE%"=="fail-clawhub" '
                'if /I "%1"=="install" if /I "%2"=="--prefix" exit /b 24\n'
                'if /I "%1"=="install" if /I "%2"=="--prefix" (\n'
                '  mkdir "%~3\\node_modules\\.bin"\n'
                '  copy /Y "%CLAWPATCH_TEST_INSTALLED_COMMAND_STUB%" '
                '"%~3\\node_modules\\.bin\\clawhub.cmd" >nul\n'
                "  exit /b 0\n"
                ")\n"
                "exit /b 25\n",
            )

        powershell = shutil.which("pwsh") or shutil.which("powershell")
        self.assertIsNotNone(powershell)
        system_root = Path(os.environ["SystemRoot"])
        install_root = root / "install"
        environment = os.environ.copy()
        # Windows environment-variable names are case-insensitive, while the
        # Python mapping can retain differently-cased duplicates. Remove the
        # inherited spellings so this test cannot accidentally use the host's
        # real PATH or Program Files npm installation.
        for key in list(environment):
            if key.casefold() in {"path", "programfiles"}:
                del environment[key]
        environment.update(
            {
                "CLAWPATCH_TEST_COMMAND_STUB": str(command_stub),
                "CLAWPATCH_TEST_INSTALLED_COMMAND_STUB": str(installed_command_stub),
                "CLAWPATCH_TEST_CLAWPATCH_VERSION": clawpatch_version,
                "CLAWPATCH_TEST_CLAWHUB_VERSION": clawhub_version,
                "CLAWPATCH_TEST_INSTALLER": str(REPOSITORY_ROOT / "scripts" / "install.ps1"),
                "CLAWPATCH_TEST_LOG": str(invocation_log),
                "CLAWPATCH_TEST_NPM_MODE": npm_mode,
                "CLAWPATCH_TEST_NPM_PREFIX": str(npm_prefix),
                "CLAWPATCH_TEST_VENV_PYTHON_STUB": str(venv_python_stub),
                "CLAWPATCH_TEST_SUPERVISOR_STUB": str(supervisor_stub),
                "CLAWPATCH_TEST_PYTHON_VERSION_FAILS": str(python_version_fails).lower(),
                "CLAWPATCH_TEST_NODE_VERSION": node_version,
                "CLAWPATCH_TEST_SOURCE": str(source_package),
                "CLAWPATCH_TEST_SOURCE_SHA256": source_sha256,
                "CLAWPATCH_TEST_INSTALL_ROOT": str(install_root),
                "CLAWPATCH_TEST_BIN_DIR": str(root / "installed-bin"),
                "PATH": os.pathsep.join((str(fake_bin), str(system_root / "System32"))),
                "ProgramFiles": str(root / "missing-program-files"),
            }
        )
        command = (
            "$global:PSNativeCommandUseErrorActionPreference = $false; "
            "& $env:CLAWPATCH_TEST_INSTALLER "
            "-Source $env:CLAWPATCH_TEST_SOURCE "
            "-InstallRoot $env:CLAWPATCH_TEST_INSTALL_ROOT "
            "-BinDir $env:CLAWPATCH_TEST_BIN_DIR "
            "-Sha256 $env:CLAWPATCH_TEST_SOURCE_SHA256"
        )
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            check=False,
            env=environment,
            text=True,
        )
        invocations = (
            [line.replace('"', "") for line in invocation_log.read_text().splitlines()]
            if invocation_log.exists()
            else []
        )
        return result, invocations, install_root

    def setUp(self) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_does_not_install_dependencies_that_are_present(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_ignores_clawhub_version(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            clawhub_version="0.18.0",
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])
        wrapper = install_root.parent / "installed-bin" / "clawpatch-supervise"
        wrapper_text = wrapper.read_text(encoding="utf-8")
        self.assertIn(str(install_root.parent / "bin"), wrapper_text)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_keeps_newer_clawpatch(self) -> None:
        result, invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_version="9.8.7",
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_isolates_mismatched_clawpatch_version(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            clawpatch_version="0.7.1",
            npm_mode="success",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            invocations,
            [
                f"install --prefix {install_root}/clawpatch --no-fund "
                "--no-audit clawpatch@latest"
            ],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_installs_missing_dependencies_and_finds_them(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            invocations,
            [
                (
                    f"install --prefix {install_root}/clawpatch --no-fund "
                    "--no-audit clawpatch@latest"
                ),
            ],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_uses_user_local_clawpatch_when_global_prefix_is_unwritable(
        self,
    ) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=True,
            npm_mode="global-unwritable",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            invocations,
            [
                (
                    f"install --prefix {install_root}/clawpatch --no-fund "
                    "--no-audit clawpatch@latest"
                )
            ],
        )
        installed_command = install_root.parent / "installed-bin" / "clawpatch"
        self.assertTrue(installed_command.is_symlink())
        self.assertEqual(
            installed_command.resolve(),
            (install_root / "clawpatch/node_modules/.bin/clawpatch").resolve(),
        )
        self.assertIn(CLAWPATCH_VERSION, result.stdout.splitlines())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_doctor_sees_installer_managed_clawpatch(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
            verify_repo=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            invocations,
            [
                f"install --prefix {install_root}/clawpatch --no-fund "
                "--no-audit clawpatch@latest"
            ],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_requires_npm_when_clawpatch_is_missing(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=True,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("npm is required to install ClawPatch.", result.stderr)
        self.assertEqual(invocations, [])
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_does_not_require_clawhub(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])
        self.assertTrue(install_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_propagates_clawpatch_install_failure(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=True,
            npm_mode="fail-clawpatch",
        )

        self.assertEqual(result.returncode, 23)
        self.assertEqual(
            invocations,
            [
                (
                    f"install --prefix {install_root}/clawpatch --no-fund "
                    "--no-audit clawpatch@latest"
                )
            ],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_never_invokes_npm_for_clawhub(self) -> None:
        result, invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="fail-clawhub",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
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

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_and_macos_installer_rejects_node_older_than_22(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            node_version="v20.18.0",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Node.js 22 or newer is required.", result.stderr)
        self.assertEqual(invocations, [])
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_accepts_wheel_with_matching_digest(self) -> None:
        wheel = Path(self._temporary_directory.name) / "clawpatch-supervise.whl"
        wheel.write_bytes(b"verified wheel")

        result, _invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            source_package=wheel,
            source_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rejects_wheel_with_mismatched_digest_before_installation(self) -> None:
        wheel = Path(self._temporary_directory.name) / "clawpatch-supervise.whl"
        wheel.write_bytes(b"tampered wheel")

        result, _invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            source_package=wheel,
            source_sha256="0" * 64,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Artifact SHA-256 mismatch", result.stderr)
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_preserves_working_command_when_upgrade_validation_fails(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "install"
        previous_supervisor = install_root / "venv.previous" / "bin" / "clawpatch-supervise"
        previous_supervisor.parent.mkdir(parents=True)
        self._write_executable(
            previous_supervisor,
            "#!/bin/sh\nprintf '0.1.20\\n'\n",
        )
        installed_command = root / "installed-bin" / "clawpatch-supervise"
        installed_command.parent.mkdir()
        installed_command.symlink_to(previous_supervisor)

        result, _invocations, actual_install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            supervisor_version_fails=True,
        )

        self.assertEqual(result.returncode, 26)
        self.assertEqual(actual_install_root, install_root)
        self.assertEqual(installed_command.resolve(), previous_supervisor)
        previous_result = subprocess.run(
            [str(installed_command), "--version"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(previous_result.returncode, 0, previous_result.stderr)
        self.assertEqual(previous_result.stdout.strip(), "0.1.20")
        self.assertEqual(list(install_root.iterdir()), [install_root / "venv.previous"])

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_requires_digest_for_custom_remote_source(self) -> None:
        result, _invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            source_package="https://example.invalid/clawpatch-supervise.whl",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("CLAWPATCH_SUPERVISE_SHA256 is required", result.stderr)
        self.assertFalse(install_root.exists())

    def test_installer_defaults_match_the_packaged_release(self) -> None:
        project = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        version = project["version"]
        linux = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        windows = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        self.assertIn(f"CLAWPATCH_SUPERVISE_VERSION:-{version}", linux)
        self.assertIn(f'[string]$Version = "{version}"', windows)
        self.assertIn("& $wrapper doctor --repo", windows)
        self.assertNotIn("& $supervisor doctor --repo", windows)

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_does_not_install_dependencies_that_are_present(self) -> None:
        result, invocations, _install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])
        wrapper = _install_root.parent / "installed-bin" / "clawpatch-supervise.cmd"
        wrapper_text = wrapper.read_text(encoding="ascii")
        self.assertIn("PYTHONUTF8=1", wrapper_text)
        self.assertIn("PYTHONIOENCODING=utf-8", wrapper_text)
        self.assertIn("NODE_DISABLE_COMPILE_CACHE=1", wrapper_text)

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_keeps_newer_clawpatch(self) -> None:
        result, invocations, _install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_version="9.8.7",
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_rejects_node_older_than_22(self) -> None:
        result, invocations, install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            node_version="v20.18.0",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Node.js 22 or newer is required.", result.stderr)
        self.assertEqual(invocations, [])
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_ignores_clawhub_version(self) -> None:
        result, invocations, _install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=True,
            clawhub_version="0.18.0",
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_isolates_mismatched_clawpatch_version(self) -> None:
        result, invocations, install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=True,
            clawpatch_version="0.7.1",
            npm_mode="success",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            invocations,
            [
                f"install --prefix {install_root / 'clawpatch'} --no-fund "
                "--no-audit clawpatch@latest"
            ],
        )

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_rejects_python_older_than_3_11_before_mutation(self) -> None:
        result, invocations, install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            python_version_fails=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11 or newer is required.", result.stderr)
        self.assertEqual(invocations, [])
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_accepts_wheel_with_matching_digest(self) -> None:
        wheel = Path(self._temporary_directory.name) / "clawpatch-supervise.whl"
        wheel.write_bytes(b"verified wheel")

        result, _invocations, _install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
            source_package=wheel,
            source_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_rejects_wheel_with_mismatched_digest(self) -> None:
        wheel = Path(self._temporary_directory.name) / "clawpatch-supervise.whl"
        wheel.write_bytes(b"tampered wheel")

        result, _invocations, install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
            source_package=wheel,
            source_sha256="0" * 64,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Artifact SHA-256 mismatch", result.stderr)
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_installs_missing_dependencies_and_finds_them(self) -> None:
        result, invocations, install_root = self._run_windows_installer(
            clawpatch_present=False,
            clawhub_present=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            invocations,
            [
                (
                    f"install --prefix {install_root / 'clawpatch'} --no-fund "
                    "--no-audit clawpatch@latest"
                ),
            ],
        )

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_requires_npm_when_clawpatch_is_missing(self) -> None:
        result, invocations, install_root = self._run_windows_installer(
            clawpatch_present=False,
            clawhub_present=True,
            npm_mode="missing",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("npm is required to install ClawPatch.", result.stderr)
        self.assertEqual(invocations, [])
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_does_not_require_clawhub(self) -> None:
        result, invocations, _install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_propagates_clawpatch_install_failure(self) -> None:
        result, invocations, _install_root = self._run_windows_installer(
            clawpatch_present=False,
            clawhub_present=True,
            npm_mode="fail-clawpatch",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("npm could not install ClawPatch.", result.stderr)
        self.assertEqual(
            invocations,
            [
                f"install --prefix {_install_root / 'clawpatch'} --no-fund "
                "--no-audit clawpatch@latest"
            ],
        )

    @unittest.skipUnless(os.name == "nt", "Windows installer test")
    def test_windows_installer_never_invokes_npm_for_clawhub(self) -> None:
        result, invocations, _install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="fail-clawhub",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])


if __name__ == "__main__":
    unittest.main()
