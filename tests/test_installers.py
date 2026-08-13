import base64
import hashlib
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Callable
from unittest.mock import patch

from _process_tree_test_support import assert_blocked_descendant_exited, wait_for_path
from clawpatch_supervise import __version__
from clawpatch_supervise.runner import CommandRunner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CLAWPATCH_VERSION = "0.7.2"
CLAWPATCH_INTEGRITY = "sha512-rhpWj6e31XJUtWKlp/MJOjdjtj+ZXc9WiLcXRk+ZaA699K++dVaYfx00dVS/QNiJBaI71IUFU6sdSPsX/nyW0g=="
CLAWHUB_VERSION = "0.19.1"
INSTALLER_TIMEOUT_SECONDS = 300


class InstallerContractTests(unittest.TestCase):
    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    def _run_installer_process(
        self,
        *,
        platform_name: str,
        installer_path: Path,
        argv: list[str],
        environment: dict[str, str],
        cwd: Path | None = None,
        timeout_seconds: float = INSTALLER_TIMEOUT_SECONDS,
        timeout_start_barrier: Callable[[subprocess.Popen[str]], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = CommandRunner().run(
            argv,
            cwd=cwd or Path.cwd(),
            env=environment,
            timeout_seconds=timeout_seconds,
            kill_process_group=True,
            timeout_start_barrier=timeout_start_barrier,
        )
        if result.timed_out:
            self.fail(
                f"{platform_name} installer timed out after {timeout_seconds:g} seconds: "
                f"{installer_path}"
            )
        return subprocess.CompletedProcess(
            argv,
            result.exit_code,
            result.stdout,
            result.stderr,
        )

    def _run_linux_installer(
        self,
        *,
        clawpatch_present: bool,
        clawhub_present: bool,
        clawpatch_command: Path | None = None,
        clawpatch_version: str = CLAWPATCH_VERSION,
        clawhub_version: str = CLAWHUB_VERSION,
        npm_mode: str = "success",
        source_package: Path | str = REPOSITORY_ROOT,
        source_sha256: str | None = None,
        supervisor_version: str = __version__,
        supervisor_version_fails: bool = False,
        clawpatch_move_fails: bool = False,
        supervisor_move_fails: bool = False,
        node_version: str = "v22.0.0",
        verify_repo: bool = False,
        relative_path: bool = False,
        exported_clawpatch_function: bool = False,
        separate_clawpatch_directory: bool = False,
        shadow_node_version: str | None = None,
        git_present: bool = True,
        runtime_dependency: str | None = None,
        process_runner=None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], Path]:
        root = Path(self._temporary_directory.name)
        doctor_clawpatch_log = root / "doctor-clawpatch.log"
        trusted_clawpatch_artifact = "verified clawpatch package"
        test_integrity = "sha512-" + base64.b64encode(
            hashlib.sha512(trusted_clawpatch_artifact.encode("ascii")).digest()
        ).decode("ascii")
        installer_path = root / "install.sh"
        installer_text = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(
            encoding="utf-8"
        )
        installer_text = installer_text.replace(CLAWPATCH_INTEGRITY, test_integrity)
        self._write_executable(installer_path, installer_text)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        command_stub = root / "command-stub"
        self._write_executable(
            command_stub,
            "#!/bin/sh\n"
            'if [ "${0##*/}:$1:$2" = "python:-m:pip" ]; then\n'
            '  printf "%s\\n" "$*" >> "$CLAWPATCH_TEST_PIP_LOG"\n'
            "  exit 0\n"
            "fi\n"
            'case "${0##*/}:$1" in\n'
            '  clawpatch:--version)\n'
            '    if [ -n "$CLAWPATCH_TEST_EXPECTED_NODE_VERSION" ]; then\n'
            '      [ "$(node --version)" = "$CLAWPATCH_TEST_EXPECTED_NODE_VERSION" ] '
            '|| exit 32\n'
            '    fi\n'
            '    printf "%s\\n" "$CLAWPATCH_TEST_CLAWPATCH_VERSION" ;;\n'
            '  clawhub:--cli-version) printf "%s\\n" "$CLAWPATCH_TEST_CLAWHUB_VERSION" ;;\n'
            '  clawpatch-supervise:--version)\n'
            '    [ "$CLAWPATCH_TEST_SUPERVISOR_VERSION_FAILS" != "true" ] || exit 26\n'
            '    printf "clawpatch-supervise %s\\n" "$CLAWPATCH_TEST_SUPERVISOR_VERSION" ;;\n'
            '  clawpatch-supervise:doctor)\n'
            '    clawpatch_path="$(command -v clawpatch)" || exit 27\n'
            '    if [ -n "$CLAWPATCH_TEST_DOCTOR_CLAWPATCH_LOG" ]; then\n'
            '      printf "%s\\n" "$clawpatch_path" >> "$CLAWPATCH_TEST_DOCTOR_CLAWPATCH_LOG"\n'
            '    fi\n'
            '    "$clawpatch_path" --version >/dev/null 2>&1 || exit 28 ;;\n'
            "esac\n"
            "exit 0\n",
        )

        for command in (
            "bash",
            "cp",
            "ln",
            "mkdir",
            "mktemp",
            "mv",
            "rm",
            "rmdir",
            "sed",
        ):
            command_path = shutil.which(command)
            self.assertIsNotNone(command_path)
            (fake_bin / command).symlink_to(command_path)
        if git_present:
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
            if separate_clawpatch_directory:
                clawpatch_bin = root / "clawpatch-bin"
                clawpatch_bin.mkdir()
                shutil.copyfile(clawpatch_command or command_stub, clawpatch_bin / "clawpatch")
                (clawpatch_bin / "clawpatch").chmod(0o755)
            else:
                (fake_bin / "clawpatch").symlink_to(clawpatch_command or command_stub)
        if clawhub_present:
            (fake_bin / "clawhub").symlink_to(command_stub)

        python = fake_bin / "python3"
        self._write_executable(
            python,
            "#!/bin/sh\n"
            'if [ "$1" = "-" ] && [ "$#" -eq 3 ] && '
            '[ "$3" = "$CLAWPATCH_SUPERVISE_BIN_DIR/clawpatch" ]; then\n'
            '  [ "$CLAWPATCH_TEST_CLAWPATCH_MOVE_FAILS" != "true" ] || exit 31\n'
            "fi\n"
            'if [ "$1" = "-" ] && [ "$#" -eq 3 ] && '
            '[ "$3" = "$CLAWPATCH_SUPERVISE_BIN_DIR/clawpatch-supervise" ]; then\n'
            '  if [ "$CLAWPATCH_TEST_PAUSE_SUPERVISOR_MOVE" = "true" ]; then\n'
            '    : > "$CLAWPATCH_TEST_PAUSE_READY"\n'
            '    while [ ! -e "$CLAWPATCH_TEST_PAUSE_RELEASE" ]; do\n'
            '      "$CLAWPATCH_TEST_REAL_PYTHON" -c "import time; time.sleep(0.01)"\n'
            '    done\n'
            '  fi\n'
            '  [ "$CLAWPATCH_TEST_SUPERVISOR_MOVE_FAILS" != "true" ] || exit 29\n'
            "fi\n"
            'if [ "$1" = "-c" ] || [ "$1" = "-" ]; then\n'
            '  exec "$CLAWPATCH_TEST_REAL_PYTHON" "$@"\n'
            "fi\n"
            'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
            '  mkdir -p "$3/bin"\n'
            '  cp "$CLAWPATCH_TEST_COMMAND_STUB" "$3/bin/python"\n'
            '  cp "$CLAWPATCH_TEST_COMMAND_STUB" "$3/bin/clawpatch-supervise"\n'
            '  metadata="$3/lib/python-test/site-packages/'
            'clawpatch_supervise.dist-info/METADATA"\n'
            '  mkdir -p "${metadata%/*}"\n'
            '  printf "Name: clawpatch-supervise\\nVersion: %s\\n" '
            '"$CLAWPATCH_TEST_SUPERVISOR_VERSION" > "$metadata"\n'
            '  if [ -n "$CLAWPATCH_TEST_RUNTIME_DEPENDENCY" ]; then\n'
            '    printf "Requires-Dist: %s\\n" "$CLAWPATCH_TEST_RUNTIME_DEPENDENCY" '
            '>> "$metadata"\n'
            "  fi\n"
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
                'if [ "$1" = "pack" ]; then\n'
                '  mkdir -p "$4"\n'
                '  artifact="$CLAWPATCH_TEST_NPM_ARTIFACT"\n'
                '  if [ "$CLAWPATCH_TEST_NPM_MODE" = "integrity-mismatch" ]; then\n'
                '    artifact="tampered clawpatch package"\n'
                "  fi\n"
                f'  printf "%s" "$artifact" > "$4/clawpatch-{CLAWPATCH_VERSION}.tgz"\n'
                "  exit 0\n"
                "fi\n"
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
                '  [ "$CLAWPATCH_TEST_NPM_MODE" != "fail-clawpatch" ] || exit 23\n'
                '  mkdir -p "$3/node_modules/.bin"\n'
                '  cp "$CLAWPATCH_TEST_INSTALLED_COMMAND_STUB" "$3/node_modules/.bin/clawpatch"\n'
                "  exit 0\n"
                "fi\n"
                "exit 25\n",
            )

        install_root = root / "install"
        installed_bin = root / "installed-bin"
        if shadow_node_version is not None:
            installed_bin.mkdir()
            self._write_executable(
                installed_bin / "node",
                f"#!/bin/sh\nprintf '%s\\n' {shadow_node_version!r}\n",
            )
        path_entries = [str(fake_bin)]
        if separate_clawpatch_directory:
            path_entries.append(str(root / "clawpatch-bin"))
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
                "CLAWPATCH_TEST_EXPECTED_NODE_VERSION": (
                    node_version if shadow_node_version is not None else ""
                ),
                "CLAWPATCH_TEST_CLAWHUB_VERSION": clawhub_version,
                "CLAWPATCH_TEST_CLAWPATCH_MOVE_FAILS": str(
                    clawpatch_move_fails
                ).lower(),
                "CLAWPATCH_TEST_DOCTOR_CLAWPATCH_LOG": str(doctor_clawpatch_log),
                "CLAWPATCH_TEST_LOG": str(invocation_log),
                "CLAWPATCH_TEST_NPM_ARTIFACT": trusted_clawpatch_artifact,
                "CLAWPATCH_TEST_NPM_MODE": npm_mode,
                "CLAWPATCH_TEST_NPM_PREFIX": str(npm_prefix),
                "CLAWPATCH_TEST_REAL_PYTHON": sys.executable,
                "CLAWPATCH_TEST_SUPERVISOR_VERSION_FAILS": str(
                    supervisor_version_fails
                ).lower(),
                "CLAWPATCH_TEST_SUPERVISOR_VERSION": supervisor_version,
                "CLAWPATCH_TEST_SUPERVISOR_MOVE_FAILS": str(
                    supervisor_move_fails
                ).lower(),
                "CLAWPATCH_TEST_NODE_VERSION": node_version,
                "CLAWPATCH_TEST_PAUSE_READY": str(root / "activation-ready"),
                "CLAWPATCH_TEST_PAUSE_RELEASE": str(root / "activation-release"),
                "CLAWPATCH_TEST_PAUSE_SUPERVISOR_MOVE": "false",
                "CLAWPATCH_TEST_PIP_LOG": str(root / "pip-invocations.log"),
                "PATH": fake_bin.name if relative_path else os.pathsep.join(path_entries),
                "CLAWPATCH_TEST_RUNTIME_DEPENDENCY": runtime_dependency or "",
            }
        )
        if exported_clawpatch_function:
            environment["BASH_FUNC_clawpatch%%"] = (
                "() {  printf '%s\\n' \"$CLAWPATCH_TEST_CLAWPATCH_VERSION\"\n}"
            )
        environment.pop("CLAWPATCH_SUPERVISE_SHA256", None)
        if source_sha256 is not None:
            environment["CLAWPATCH_SUPERVISE_SHA256"] = source_sha256
        if verify_repo:
            environment["CLAWPATCH_SUPERVISE_VERIFY_REPO"] = str(root)
        if process_runner is None:
            result = self._run_installer_process(
                platform_name="Linux/macOS",
                installer_path=installer_path,
                argv=[str(installer_path)],
                environment=environment,
                cwd=root if relative_path else None,
            )
        else:
            result = process_runner(installer_path, environment)
        invocations = (
            invocation_log.read_text(encoding="utf-8").splitlines()
            if invocation_log.exists()
            else []
        )
        return result, invocations, install_root

    def _assert_staged_clawpatch_install(
        self, invocations: list[str], install_root: Path
    ) -> Path:
        self.assertEqual(len(invocations), 2)
        pack_prefix = "pack --ignore-scripts --pack-destination "
        pack_suffix = f" clawpatch@{CLAWPATCH_VERSION}"
        self.assertTrue(invocations[0].startswith(pack_prefix), invocations)
        self.assertTrue(invocations[0].endswith(pack_suffix), invocations)
        download_root = Path(invocations[0][len(pack_prefix) : -len(pack_suffix)])
        prefix = "install --prefix "
        suffix = (
            " --no-fund --no-audit --ignore-scripts "
            f"{download_root}/clawpatch-{CLAWPATCH_VERSION}.tgz"
        )
        self.assertTrue(invocations[1].startswith(prefix), invocations)
        self.assertTrue(invocations[1].endswith(suffix), invocations)
        staged_root = Path(invocations[1][len(prefix) : -len(suffix)])
        self.assertEqual(staged_root.parent, install_root)
        self.assertTrue(staged_root.name.startswith("clawpatch."), staged_root)
        return staged_root

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
        supervisor_version: str = __version__,
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
            'if "%1"=="--version" echo clawpatch-supervise %CLAWPATCH_TEST_SUPERVISOR_VERSION%\n'
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
                f'if /I "%1"=="install" if /I "%~6"=="clawpatch@{CLAWPATCH_VERSION}" exit /b 23\n'
                'if /I "%1"=="install" if /I "%2"=="--prefix" '
                f'if /I "%~6"=="clawpatch@{CLAWPATCH_VERSION}" (\n'
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
                "CLAWPATCH_TEST_SUPERVISOR_VERSION": supervisor_version,
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
        installer_path = REPOSITORY_ROOT / "scripts" / "install.ps1"
        result = self._run_installer_process(
            platform_name="Windows",
            installer_path=installer_path,
            argv=[
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            environment=environment,
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

    @unittest.skipUnless(os.name == "posix", "POSIX process-group integration")
    def test_installer_timeout_reports_context_and_kills_descendants(self) -> None:
        root = Path(self._temporary_directory.name)
        installer_path = root / "timeout-installer"
        ready = root / "descendant-ready.txt"
        release = root / "descendant-release.txt"
        escaped = root / "descendant-wrote.txt"
        child_source = (
            "import os, signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(0.3)\n"
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

        with self.assertRaises(AssertionError) as raised:
            self._run_installer_process(
                platform_name="Linux/macOS",
                installer_path=installer_path,
                argv=[
                    sys.executable,
                    "-c",
                    parent_source,
                    child_source,
                    str(ready),
                    str(release),
                    str(escaped),
                ],
                environment=os.environ.copy(),
                cwd=root,
                timeout_seconds=0.2,
                timeout_start_barrier=lambda _process: wait_for_path(ready),
            )

        self.assertIn("Linux/macOS", str(raised.exception))
        self.assertIn(str(installer_path), str(raised.exception))
        self.assertIn("0.2 seconds", str(raised.exception))
        self.assertTrue(ready.is_file())
        assert_blocked_descendant_exited(
            ready=ready,
            release=release,
            sentinel=escaped,
        )

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
            clawpatch_version="clawpatch 0.7.3",
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installed_wrapper_uses_validated_node(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
            separate_clawpatch_directory=True,
            shadow_node_version="v20.18.0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])
        wrapper = install_root.parent / "installed-bin" / "clawpatch-supervise"
        installed_environment = os.environ.copy()
        installed_environment.update(
            {
                "CLAWPATCH_TEST_CLAWPATCH_VERSION": CLAWPATCH_VERSION,
                "CLAWPATCH_TEST_EXPECTED_NODE_VERSION": "v22.0.0",
                "CLAWPATCH_TEST_NODE_VERSION": "v22.0.0",
            }
        )
        installed_result = subprocess.run(
            [str(wrapper), "doctor"],
            capture_output=True,
            check=False,
            env=installed_environment,
            text=True,
        )
        self.assertEqual(installed_result.returncode, 0, installed_result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rejects_exported_clawpatch_function(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
            npm_mode="missing",
            exported_clawpatch_function=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("npm is required to install ClawPatch.", result.stderr)
        self.assertEqual(invocations, [])
        self.assertFalse(install_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_canonicalizes_clawpatch_from_relative_path(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
            relative_path=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])
        wrapper = install_root.parent / "installed-bin" / "clawpatch-supervise"
        wrapper_text = wrapper.read_text(encoding="utf-8")
        self.assertIn(str(install_root.parent / "bin"), wrapper_text)
        self.assertNotIn("export PATH=bin:", wrapper_text)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_normalizes_relative_install_paths(self) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "relative-install"
        bin_dir = root / "relative-bin"

        def install_from_root(
            installer_path: Path, environment: dict[str, str]
        ) -> subprocess.CompletedProcess[str]:
            environment["CLAWPATCH_SUPERVISE_HOME"] = install_root.name
            environment["CLAWPATCH_SUPERVISE_BIN_DIR"] = bin_dir.name
            return self._run_installer_process(
                platform_name="Linux/macOS",
                installer_path=installer_path,
                argv=[str(installer_path)],
                environment=environment,
                cwd=root,
            )

        result, _invocations, _default_install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
            process_runner=install_from_root,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        unrelated_directory = root / "unrelated"
        unrelated_directory.mkdir()
        installed_result = subprocess.run(
            [str(bin_dir / "clawpatch-supervise"), "doctor"],
            capture_output=True,
            check=False,
            cwd=unrelated_directory,
            text=True,
        )
        self.assertEqual(installed_result.returncode, 0, installed_result.stderr)
        clawpatch_result = subprocess.run(
            [str(bin_dir / "clawpatch"), "--version"],
            capture_output=True,
            check=False,
            cwd=unrelated_directory,
            text=True,
        )
        self.assertEqual(clawpatch_result.returncode, 0, clawpatch_result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_isolates_mismatched_clawpatch_version(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            clawpatch_version="0.7.1",
            npm_mode="success",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_staged_clawpatch_install(invocations, install_root)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rejects_clawpatch_output_with_unrelated_version(
        self,
    ) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_version="dependency 99.0.0\nclawpatch 0.1.0",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_staged_clawpatch_install(invocations, install_root)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_isolates_prerelease_clawpatch_version(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_version="clawpatch 0.7.2-rc.1",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_staged_clawpatch_install(invocations, install_root)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_installs_missing_dependencies_and_finds_them(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self._assert_staged_clawpatch_install(invocations, install_root)

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
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        installed_command = install_root.parent / "installed-bin" / "clawpatch"
        self.assertTrue(installed_command.is_symlink())
        self.assertEqual(
            installed_command.resolve(),
            (staged_root / "node_modules/.bin/clawpatch").resolve(),
        )
        self.assertIn(CLAWPATCH_VERSION, result.stdout.splitlines())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_doctor_sees_installer_managed_clawpatch(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_version="0.7.1",
            verify_repo=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        managed_clawpatch = staged_root / "node_modules/.bin/clawpatch"
        resolution_log = install_root.parent / "doctor-clawpatch.log"
        self.assertEqual(
            resolution_log.read_text(encoding="utf-8").splitlines(),
            [str(managed_clawpatch)],
        )

        wrapper = install_root.parent / "installed-bin" / "clawpatch-supervise"
        installed_result = subprocess.run(
            [str(wrapper), "doctor"],
            capture_output=True,
            check=False,
            env={
                **os.environ,
                "CLAWPATCH_TEST_DOCTOR_CLAWPATCH_LOG": str(resolution_log),
            },
            text=True,
        )
        self.assertEqual(installed_result.returncode, 0, installed_result.stderr)
        self.assertEqual(
            resolution_log.read_text(encoding="utf-8").splitlines(),
            [str(managed_clawpatch), str(managed_clawpatch)],
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
    def test_linux_installer_does_not_require_git(self) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
            git_present=False,
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
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        self.assertFalse(staged_root.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rejects_clawpatch_tarball_with_mismatched_integrity(
        self,
    ) -> None:
        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
            npm_mode="integrity-mismatch",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("ClawPatch artifact SHA-512 mismatch", result.stderr)
        self.assertEqual(len(invocations), 1)
        self.assertTrue(invocations[0].startswith("pack --ignore-scripts "))
        self.assertFalse(any(command.startswith("install ") for command in invocations))
        self.assertEqual(list(install_root.glob("clawpatch.*")), [])

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
    def test_linux_installer_rejects_runtime_dependencies_before_activation(self) -> None:
        root = Path(self._temporary_directory.name)

        result, _invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            runtime_dependency="example-dependency>=1",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("must have no Python runtime dependencies", result.stderr)
        self.assertIn("example-dependency>=1", result.stderr)
        self.assertIn(
            "--disable-pip-version-check --no-deps --upgrade",
            (root / "pip-invocations.log").read_text(encoding="utf-8"),
        )
        self.assertFalse((root / "installed-bin" / "clawpatch-supervise").exists())

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
    def test_linux_installer_preserves_managed_clawpatch_when_artifact_validation_fails(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "install"
        previous_clawpatch = (
            install_root / "clawpatch" / "node_modules" / ".bin" / "clawpatch"
        )
        previous_clawpatch.parent.mkdir(parents=True)
        self._write_executable(
            previous_clawpatch,
            "#!/bin/sh\nprintf '0.7.1\\n'\n",
        )
        installed_bin = root / "installed-bin"
        installed_bin.mkdir()
        installed_clawpatch = installed_bin / "clawpatch"
        installed_clawpatch.symlink_to(previous_clawpatch)
        installed_supervisor = installed_bin / "clawpatch-supervise"
        previous_wrapper = (
            "#!/bin/sh\n"
            f'export PATH="{previous_clawpatch.parent}:$PATH"\n'
            'if [ "$1" = "--dependency-version" ]; then exec clawpatch --version; fi\n'
            "printf '0.1.20\\n'\n"
        )
        self._write_executable(installed_supervisor, previous_wrapper)
        wheel = root / "clawpatch-supervise.whl"
        wheel.write_bytes(b"tampered wheel")

        result, invocations, actual_install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_command=previous_clawpatch,
            source_package=wheel,
            source_sha256="0" * 64,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Artifact SHA-256 mismatch", result.stderr)
        self.assertEqual(actual_install_root, install_root)
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        self.assertFalse(staged_root.exists())
        self.assertEqual(installed_clawpatch.resolve(), previous_clawpatch.resolve())
        self.assertEqual(installed_supervisor.read_text(encoding="utf-8"), previous_wrapper)
        dependency = subprocess.run(
            [str(installed_supervisor), "--dependency-version"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(dependency.returncode, 0, dependency.stderr)
        self.assertEqual(dependency.stdout.strip(), "0.7.1")
        self.assertEqual(
            sorted(path.name for path in install_root.iterdir()),
            ["clawpatch"],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rolls_back_commands_when_clawpatch_activation_fails(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "install"
        previous_clawpatch = (
            install_root
            / "clawpatch.previous"
            / "node_modules"
            / ".bin"
            / "clawpatch"
        )
        previous_clawpatch.parent.mkdir(parents=True)
        self._write_executable(previous_clawpatch, "#!/bin/sh\nprintf '0.7.1\\n'\n")
        installed_bin = root / "installed-bin"
        installed_bin.mkdir()
        installed_clawpatch = installed_bin / "clawpatch"
        previous_clawpatch_target = os.path.relpath(previous_clawpatch, installed_bin)
        installed_clawpatch.symlink_to(previous_clawpatch_target)
        previous_venv = install_root / "venv.0.1.20.previous"
        previous_supervisor = previous_venv / "bin" / "clawpatch-supervise"
        previous_supervisor.parent.mkdir(parents=True)
        self._write_executable(previous_supervisor, "#!/bin/sh\nprintf '0.1.20\\n'\n")
        installed_supervisor = installed_bin / "clawpatch-supervise"
        previous_supervisor_content = (
            f'#!/bin/sh\nexec {previous_supervisor} "$@"\n'.encode()
        )
        installed_supervisor.write_bytes(previous_supervisor_content)
        installed_supervisor.chmod(0o751)

        result, invocations, actual_install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_command=previous_clawpatch,
            clawpatch_move_fails=True,
        )

        self.assertEqual(result.returncode, 31, result.stderr)
        self.assertEqual(actual_install_root, install_root)
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        self.assertFalse(staged_root.exists())
        self.assertTrue(installed_clawpatch.is_symlink())
        self.assertEqual(os.readlink(installed_clawpatch), previous_clawpatch_target)
        self.assertEqual(installed_supervisor.read_bytes(), previous_supervisor_content)
        self.assertEqual(installed_supervisor.stat().st_mode & 0o777, 0o751)
        self.assertEqual(
            sorted(path.name for path in installed_bin.iterdir()),
            ["clawpatch", "clawpatch-supervise"],
        )
        self.assertEqual(
            sorted(path.name for path in install_root.iterdir()),
            ["clawpatch.previous", "venv.0.1.20.previous"],
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rolls_back_commands_when_supervisor_activation_fails(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "install"
        previous_clawpatch = (
            install_root / "clawpatch" / "node_modules" / ".bin" / "clawpatch"
        )
        previous_clawpatch.parent.mkdir(parents=True)
        previous_clawpatch_content = "#!/bin/sh\nprintf '0.7.1\\n'\n"
        self._write_executable(previous_clawpatch, previous_clawpatch_content)
        installed_bin = root / "installed-bin"
        installed_bin.mkdir()
        installed_clawpatch = installed_bin / "clawpatch"
        installed_clawpatch.symlink_to(previous_clawpatch)
        previous_venv = install_root / "venv.0.1.20.previous"
        previous_supervisor = previous_venv / "bin" / "clawpatch-supervise"
        previous_supervisor.parent.mkdir(parents=True)
        self._write_executable(previous_supervisor, "#!/bin/sh\nprintf '0.1.20\\n'\n")
        installed_supervisor = installed_bin / "clawpatch-supervise"
        previous_supervisor_content = (
            f'#!/bin/sh\nexec {previous_supervisor} "$@"\n'
        )
        self._write_executable(installed_supervisor, previous_supervisor_content)

        result, invocations, actual_install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_command=previous_clawpatch,
            supervisor_move_fails=True,
        )

        self.assertEqual(result.returncode, 29, result.stderr)
        self.assertEqual(actual_install_root, install_root)
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        self.assertFalse(staged_root.exists())
        self.assertTrue(installed_clawpatch.is_symlink())
        self.assertEqual(installed_clawpatch.resolve(), previous_clawpatch.resolve())
        self.assertEqual(
            previous_clawpatch.read_text(encoding="utf-8"),
            previous_clawpatch_content,
        )
        self.assertEqual(
            installed_supervisor.read_text(encoding="utf-8"),
            previous_supervisor_content,
        )
        self.assertEqual(
            sorted(path.name for path in install_root.iterdir()),
            ["clawpatch", "venv.0.1.20.previous"],
        )
        self.assertTrue(previous_venv.exists())
        self.assertFalse(
            (installed_bin / ".clawpatch-supervise.install.lock").exists()
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_removes_new_commands_when_supervisor_activation_fails(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)

        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
            supervisor_move_fails=True,
        )

        self.assertEqual(result.returncode, 29, result.stderr)
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        self.assertFalse(staged_root.exists())
        installed_bin = root / "installed-bin"
        self.assertFalse(os.path.lexists(installed_bin / "clawpatch"))
        self.assertFalse(os.path.lexists(installed_bin / "clawpatch-supervise"))
        self.assertEqual(
            sorted(path.name for path in installed_bin.iterdir()),
            [],
        )
        self.assertEqual(list(install_root.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rejects_symlink_at_install_lock_without_modifying_target(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)
        sentinel = root / "sentinel"
        sentinel.write_text("do not modify\n", encoding="utf-8")
        installed_bin = root / "installed-bin"
        installed_bin.mkdir()
        lock_path = installed_bin / ".clawpatch-supervise.install.lock"
        lock_path.symlink_to(sentinel)

        result, _invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Install lock path is not a directory", result.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not modify\n")
        self.assertTrue(lock_path.is_symlink())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_times_out_on_stale_install_lock(self) -> None:
        root = Path(self._temporary_directory.name)
        installed_bin = root / "installed-bin"
        lock_path = installed_bin / ".clawpatch-supervise.install.lock"
        lock_path.mkdir(parents=True)

        def install_with_short_lock_timeout(
            installer_path: Path, environment: dict[str, str]
        ) -> subprocess.CompletedProcess[str]:
            environment["CLAWPATCH_SUPERVISE_INSTALL_LOCK_TIMEOUT_SECONDS"] = "1"
            return self._run_installer_process(
                platform_name="Linux/macOS",
                installer_path=installer_path,
                argv=[str(installer_path)],
                environment=environment,
                timeout_seconds=5,
            )

        result, _invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            process_runner=install_with_short_lock_timeout,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("Timed out after 1s waiting for install lock", result.stderr)
        self.assertIn("If no installer is running, remove it and retry", result.stderr)
        self.assertTrue(lock_path.is_dir())
        self.assertNotIn("Installed command:", result.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_retains_backup_when_rollback_restore_fails(self) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "install"
        previous_clawpatch = (
            install_root / "clawpatch" / "node_modules" / ".bin" / "clawpatch"
        )
        previous_clawpatch.parent.mkdir(parents=True)
        self._write_executable(previous_clawpatch, "#!/bin/sh\nprintf '0.7.1\\n'\n")
        installed_bin = root / "installed-bin"
        installed_bin.mkdir()
        installed_clawpatch = installed_bin / "clawpatch"
        installed_clawpatch.symlink_to(previous_clawpatch)

        def fail_clawpatch_restore(
            installer_path: Path, environment: dict[str, str]
        ) -> subprocess.CompletedProcess[str]:
            fake_mv = Path(environment["PATH"]) / "mv"
            real_mv = fake_mv.resolve()
            fake_mv.unlink()
            self._write_executable(
                fake_mv,
                "#!/bin/sh\n"
                'case "$2" in\n'
                '  */.clawpatch.previous.*) exit 30 ;;\n'
                "esac\n"
                f'exec "{real_mv}" "$@"\n',
            )
            return self._run_installer_process(
                platform_name="Linux/macOS",
                installer_path=installer_path,
                argv=[str(installer_path)],
                environment=environment,
            )

        result, invocations, actual_install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_command=previous_clawpatch,
            supervisor_move_fails=True,
            process_runner=fail_clawpatch_restore,
        )

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(actual_install_root, install_root)
        self.assertFalse(self._assert_staged_clawpatch_install(invocations, install_root).exists())
        retained_backups = list(installed_bin.glob(".clawpatch.previous.*"))
        self.assertEqual(len(retained_backups), 1)
        retained_backup = retained_backups[0]
        self.assertTrue(retained_backup.is_symlink())
        self.assertEqual(retained_backup.resolve(), previous_clawpatch.resolve())
        self.assertIn(str(retained_backup), result.stderr)
        self.assertIn("Rollback failed", result.stderr)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_serializes_shared_command_directory_across_install_roots(
        self,
    ) -> None:
        interrupted_returncode = None

        def run_concurrently(
            installer_path: Path, environment: dict[str, str]
        ) -> subprocess.CompletedProcess[str]:
            nonlocal interrupted_returncode
            first_environment = environment.copy()
            first_install_root = Path(environment["CLAWPATCH_SUPERVISE_HOME"]).with_name(
                "first-install"
            )
            first_environment["CLAWPATCH_SUPERVISE_HOME"] = str(first_install_root)
            first_environment["CLAWPATCH_TEST_PAUSE_SUPERVISOR_MOVE"] = "true"
            first = subprocess.Popen(
                [str(installer_path)],
                env=first_environment,
                start_new_session=True,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            ready = Path(environment["CLAWPATCH_TEST_PAUSE_READY"])
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                if first.poll() is not None:
                    self.fail(
                        f"first installer exited before activation: {first.stderr.read()}"
                    )
                time.sleep(0.01)
            self.assertTrue(ready.exists(), "first installer did not reach activation")

            second = subprocess.Popen(
                [str(installer_path)],
                env=environment,
                stderr=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            time.sleep(0.2)
            self.assertIsNone(
                second.poll(), "second installer bypassed the activation lock"
            )

            os.killpg(first.pid, signal.SIGTERM)
            first.communicate(timeout=10)
            interrupted_returncode = first.returncode
            try:
                second_stdout, second_stderr = second.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                second.kill()
                second.communicate()
                self.fail("second installer did not acquire the released activation lock")
            return subprocess.CompletedProcess(
                [str(installer_path)],
                second.returncode,
                second_stdout,
                second_stderr,
            )

        result, invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            process_runner=run_concurrently,
        )

        self.assertNotEqual(interrupted_returncode, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(invocations, [])
        environments = list(install_root.glob("venv.*"))
        self.assertEqual(len(environments), 1)
        installed_command = install_root.parent / "installed-bin" / "clawpatch-supervise"
        self.assertIn(
            str(environments[0] / "bin" / "clawpatch-supervise"),
            installed_command.read_text(encoding="utf-8"),
        )

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
        self.assertEqual(installed_command.resolve(), previous_supervisor.resolve())
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
    def test_linux_installer_rejects_stale_supervisor_candidate_before_activation(
        self,
    ) -> None:
        result, _invocations, install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            supervisor_version="0.1.20",
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            f"expected clawpatch-supervise {__version__}, found clawpatch-supervise 0.1.20",
            result.stderr,
        )
        self.assertFalse((install_root.parent / "installed-bin" / "clawpatch-supervise").exists())
        self.assertEqual(list(install_root.iterdir()), [])

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_retains_superseded_managed_environment_after_activation(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "install"
        previous_venv = install_root / "venv.0.1.27.previous"
        previous_supervisor = previous_venv / "bin" / "clawpatch-supervise"
        previous_supervisor.parent.mkdir(parents=True)
        self._write_executable(previous_supervisor, "#!/bin/sh\nprintf '0.1.27\\n'\n")
        installed_command = root / "installed-bin" / "clawpatch-supervise"
        installed_command.parent.mkdir()
        self._write_executable(
            installed_command,
            f'#!/bin/sh\nexec {previous_supervisor} "$@"\n',
        )
        preserved_wrapper = root / "preserved-clawpatch-supervise"
        shutil.copy2(installed_command, preserved_wrapper)

        result, _invocations, actual_install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual_install_root, install_root)
        self.assertTrue(previous_venv.exists())
        preserved_result = subprocess.run(
            [str(preserved_wrapper), "--version"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(preserved_result.returncode, 0, preserved_result.stderr)
        self.assertEqual(preserved_result.stdout.strip(), "0.1.27")
        environments = list(install_root.glob("venv.*"))
        self.assertEqual(len(environments), 2)
        activated_environment = next(
            environment for environment in environments if environment != previous_venv
        )
        self.assertIn(
            str(activated_environment / "bin" / "clawpatch-supervise"),
            installed_command.read_text(encoding="utf-8"),
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_retains_superseded_managed_clawpatch_root(self) -> None:
        root = Path(self._temporary_directory.name)
        install_root = root / "install"
        previous_root = install_root / "clawpatch.previous"
        previous_package_command = (
            previous_root / "node_modules" / "clawpatch" / "bin" / "clawpatch"
        )
        previous_package_command.parent.mkdir(parents=True)
        self._write_executable(
            previous_package_command, "#!/bin/sh\nprintf '0.7.1\\n'\n"
        )
        previous_clawpatch = previous_root / "node_modules" / ".bin" / "clawpatch"
        previous_clawpatch.parent.mkdir()
        previous_clawpatch.symlink_to(Path("../clawpatch/bin/clawpatch"))
        unrelated_directory = install_root / "unrelated"
        unrelated_directory.mkdir()
        installed_clawpatch = root / "installed-bin" / "clawpatch"
        installed_clawpatch.parent.mkdir()
        installed_clawpatch.symlink_to(previous_clawpatch)
        preserved_clawpatch = root / "preserved-clawpatch"
        preserved_clawpatch.symlink_to(previous_clawpatch)

        result, invocations, actual_install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            clawpatch_command=previous_clawpatch,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(actual_install_root, install_root)
        staged_root = self._assert_staged_clawpatch_install(invocations, install_root)
        self.assertTrue(previous_root.exists())
        preserved_result = subprocess.run(
            [str(preserved_clawpatch), "--version"],
            capture_output=True,
            check=False,
            text=True,
        )
        self.assertEqual(preserved_result.returncode, 0, preserved_result.stderr)
        self.assertEqual(preserved_result.stdout.strip(), "0.7.1")
        self.assertTrue(staged_root.exists())
        self.assertTrue(unrelated_directory.exists())
        self.assertEqual(
            installed_clawpatch.resolve(),
            (staged_root / "node_modules" / ".bin" / "clawpatch").resolve(),
        )

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_preserves_superseded_environment_outside_install_root(
        self,
    ) -> None:
        root = Path(self._temporary_directory.name)
        external_venv = root / "external" / "venv.0.1.27.previous"
        external_supervisor = external_venv / "bin" / "clawpatch-supervise"
        external_supervisor.parent.mkdir(parents=True)
        self._write_executable(external_supervisor, "#!/bin/sh\nexit 0\n")
        installed_command = root / "installed-bin" / "clawpatch-supervise"
        installed_command.parent.mkdir()
        self._write_executable(
            installed_command,
            f'#!/bin/sh\nexec {external_supervisor} "$@"\n',
        )

        result, _invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(external_venv.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rejects_supervisor_command_directory(self) -> None:
        command_directory = (
            Path(self._temporary_directory.name)
            / "installed-bin"
            / "clawpatch-supervise"
        )
        command_directory.mkdir(parents=True)

        result, _invocations, _install_root = self._run_linux_installer(
            clawpatch_present=True,
            clawhub_present=False,
            npm_mode="missing",
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"Command destination is a directory: {command_directory}",
            result.stderr,
        )
        self.assertEqual(list(command_directory.iterdir()), [])
        self.assertNotIn("Installed command:", result.stdout)

    @unittest.skipUnless(os.name == "posix", "POSIX installer test")
    def test_linux_installer_rejects_clawpatch_command_directory(self) -> None:
        command_directory = (
            Path(self._temporary_directory.name) / "installed-bin" / "clawpatch"
        )
        command_directory.mkdir(parents=True)

        result, _invocations, _install_root = self._run_linux_installer(
            clawpatch_present=False,
            clawhub_present=False,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            f"Command destination is a directory: {command_directory}",
            result.stderr,
        )
        self.assertEqual(list(command_directory.iterdir()), [])
        self.assertNotIn("Installed command:", result.stdout)

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
        linux = (REPOSITORY_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        windows = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")

        self.assertIn(f"CLAWPATCH_SUPERVISE_VERSION:-{__version__}", linux)
        self.assertIn(f'[string]$Version = "{__version__}"', windows)
        self.assertIn(f'readonly release_clawpatch_version="{CLAWPATCH_VERSION}"', linux)
        self.assertIn(
            f'readonly release_clawpatch_integrity_0_7_2="{CLAWPATCH_INTEGRITY}"',
            linux,
        )
        self.assertIn('"clawpatch@$release_clawpatch_version"', linux)
        self.assertIn('--ignore-scripts "$clawpatch_package"', linux)
        self.assertIn(f'"clawpatch@$ReleaseClawPatchVersion"', windows)
        self.assertIn(f'$ReleaseClawPatchVersion = "{CLAWPATCH_VERSION}"', windows)
        self.assertNotIn("clawpatch@latest", linux)
        self.assertNotIn("clawpatch@latest", windows)
        self.assertIn("& $wrapper doctor --repo", windows)
        self.assertNotIn("& $supervisor doctor --repo", windows)

    def test_repository_has_no_hosted_workflow_files(self) -> None:
        workflow_root = REPOSITORY_ROOT / ".github" / "workflows"

        workflow_files = sorted(
            path.relative_to(workflow_root)
            for path in workflow_root.rglob("*")
            if path.is_file()
        )
        self.assertEqual(workflow_files, [])

    def test_windows_installer_checks_compatibility_before_install_root_mutation(self) -> None:
        windows = (REPOSITORY_ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        first_install_root_mutation = windows.index(
            "& $npmPath install --prefix $clawpatchRoot"
        )

        for compatibility_check in (
            "$pythonVersionOutput =",
            "$nodeVersionOutput =",
            "Test-CompatibleClawPatch $clawpatch",
        ):
            with self.subTest(compatibility_check=compatibility_check):
                self.assertLess(windows.index(compatibility_check), first_install_root_mutation)

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
    def test_windows_installer_rejects_stale_supervisor_candidate_before_activation(
        self,
    ) -> None:
        result, _invocations, install_root = self._run_windows_installer(
            clawpatch_present=True,
            clawhub_present=True,
            npm_mode="missing",
            supervisor_version="0.1.20",
        )

        self.assertNotEqual(result.returncode, 0)
        normalized_stderr = " ".join(result.stderr.split())
        self.assertIn(
            f"expected clawpatch-supervise {__version__}, found clawpatch-supervise 0.1.20",
            normalized_stderr,
        )
        self.assertFalse(
            (install_root.parent / "installed-bin" / "clawpatch-supervise.cmd").exists()
        )

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
                f"--no-audit clawpatch@{CLAWPATCH_VERSION}"
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
                    f"--no-audit clawpatch@{CLAWPATCH_VERSION}"
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
                f"--no-audit clawpatch@{CLAWPATCH_VERSION}"
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
