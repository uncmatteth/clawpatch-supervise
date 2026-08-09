from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Mapping
from unittest.mock import patch

from clawpatch_supervise.errors import SafetyError
from clawpatch_supervise.validation_services import (
    PostgresTestContract,
    PythonTestContract,
    _provision_python_test_environment,
    _provision_postgres_test_environment,
    _run_command,
)


class DisposablePythonValidationTests(unittest.TestCase):
    @patch.dict(os.environ, {"GITHUB_TOKEN": "github-secret"})
    def test_default_runner_does_not_restore_omitted_host_environment(self) -> None:
        safe_env = {
            name: os.environ[name]
            for name in ("PATH", "SYSTEMROOT")
            if name in os.environ
        }

        result = _run_command(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('GITHUB_TOKEN', 'absent'))",
            ],
            cwd=Path.cwd(),
            timeout=30,
            env=safe_env,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "absent")

    @patch.dict(
        "clawpatch_supervise.validation_services.os.environ",
        {
            "PATH": os.environ.get("PATH", ""),
            "DATABASE_URL": "postgresql://production.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "true",
            "GITHUB_TOKEN": "github-secret",
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "PIP_INDEX_URL": "https://user:password@packages.invalid/simple",
            "SSH_AUTH_SOCK": "/tmp/host-agent.sock",
        },
        clear=True,
    )
    def test_every_python_provisioning_command_uses_owned_sanitized_environment(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        def run(
            argv: list[str],
            *,
            cwd: Path,
            timeout: int,
            env: Mapping[str, str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append((argv, dict(env)))
            if argv[:3] == [sys.executable, "-m", "venv"]:
                environment = Path(argv[3])
                executable_dir = environment / ("Scripts" if os.name == "nt" else "bin")
                executable_dir.mkdir(parents=True)
                python = executable_dir / ("python.exe" if os.name == "nt" else "python")
                python.touch()
            return subprocess.CompletedProcess(argv, 0, "", "")

        contract = PythonTestContract(Path("pyproject.toml"), ())
        with tempfile.TemporaryDirectory() as temp:
            temporary_root = Path(temp)
            with _provision_python_test_environment(
                temporary_root,
                contract,
                run=run,
                progress=None,
                temporary_root=temporary_root,
            ):
                self.assertEqual(len(calls), 3)
                credential_names = {
                    "DATABASE_URL",
                    "BTT_ALLOW_DATABASE_RESET",
                    "GITHUB_TOKEN",
                    "AWS_SECRET_ACCESS_KEY",
                    "PIP_INDEX_URL",
                    "SSH_AUTH_SOCK",
                }
                for _argv, environment in calls:
                    self.assertTrue(credential_names.isdisjoint(environment))
                    for name in ("HOME", "USERPROFILE", "PIP_CACHE_DIR", "TMPDIR", "TMP", "TEMP"):
                        self.assertTrue(
                            Path(environment[name]).is_relative_to(temporary_root),
                            f"{name} escaped the supervisor-owned temporary root",
                        )
                    self.assertEqual(environment["PIP_CONFIG_FILE"], os.devnull)
                    self.assertEqual(environment["PIP_NO_INPUT"], "1")

        self.assertEqual(calls[0][0][1:3], ["-m", "venv"])
        self.assertIn("pytest>=8,<10", calls[1][0])
        self.assertEqual(calls[2][0][-1], ".")


class DisposablePostgresValidationTests(unittest.TestCase):
    @patch.dict(
        "clawpatch_supervise.validation_services.os.environ",
        {
            "TEST_DATABASE_URL": "postgresql://external.invalid/live",
            "BTT_ALLOW_DATABASE_RESET": "true",
        },
        clear=True,
    )
    @patch(
        "clawpatch_supervise.validation_services._verified_postgres_image",
        return_value="postgres:16",
    )
    @patch("clawpatch_supervise.validation_services._compose_contract")
    def test_inherited_reset_capable_database_is_replaced_by_owned_disposable_service(
        self, compose_contract, _verified_postgres_image
    ) -> None:
        compose_contract.return_value = PostgresTestContract(
            compose_file=Path("compose.yaml"),
            image="postgres:16",
            url_env="TEST_DATABASE_URL",
            reset_envs=("BTT_ALLOW_DATABASE_RESET",),
        )
        calls: list[list[str]] = []
        env_files: list[Path] = []

        def run(
            argv: list[str],
            *,
            cwd: Path,
            timeout: int,
            env: Mapping[str, str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                self.assertFalse(any("disposable-password" in argument for argument in argv))
                env_file = Path(argv[argv.index("--env-file") + 1])
                env_files.append(env_file)
                if os.name != "nt":
                    self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    env_file.read_text(encoding="utf-8"),
                    "POSTGRES_PASSWORD=disposable-password\n",
                )
                return subprocess.CompletedProcess(argv, 0, "a" * 64 + "\n", "")
            if argv[:2] == ["docker", "port"]:
                return subprocess.CompletedProcess(argv, 0, "127.0.0.1:49152\n", "")
            if argv[:2] in (["docker", "exec"], ["docker", "rm"]):
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail(f"unexpected command: {argv}")

        with tempfile.TemporaryDirectory() as temp:
            with _provision_postgres_test_environment(
                Path(temp),
                run=run,
                password_factory=lambda: "disposable-password",
            ) as child_env:
                self.assertEqual(
                    child_env["TEST_DATABASE_URL"],
                    "postgresql://manageroo:disposable-password@127.0.0.1:49152/manageroo_test",
                )
                self.assertEqual(child_env["BTT_ALLOW_DATABASE_RESET"], "true")

        self.assertTrue(any(argv[:2] == ["docker", "run"] for argv in calls))
        self.assertTrue(any(argv[:3] == ["docker", "rm", "-f"] for argv in calls))
        self.assertEqual(len(env_files), 1)
        self.assertFalse(env_files[0].exists())

    @patch(
        "clawpatch_supervise.validation_services._verified_postgres_image",
        return_value="postgres:16",
    )
    @patch("clawpatch_supervise.validation_services._compose_contract")
    def test_malformed_container_id_output_still_removes_exact_container_name(
        self, compose_contract, _verified_postgres_image
    ) -> None:
        compose_contract.return_value = PostgresTestContract(
            compose_file=Path("compose.yaml"),
            image="postgres:16",
            url_env="TEST_DATABASE_URL",
            reset_envs=("BTT_ALLOW_DATABASE_RESET",),
        )
        calls: list[list[str]] = []

        def run(
            argv: list[str],
            *,
            cwd: Path,
            timeout: int,
            env: Mapping[str, str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                return subprocess.CompletedProcess(argv, 0, "a" * 64 + "\nunexpected output\n", "")
            if argv[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail(f"unexpected command: {argv}")

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(
                SafetyError,
                "Docker returned an invalid disposable PostgreSQL container ID",
            ):
                with _provision_postgres_test_environment(Path(temp), run=run):
                    self.fail("malformed container ID output must not yield an environment")

        docker_run = next(argv for argv in calls if argv[:2] == ["docker", "run"])
        container_name = docker_run[docker_run.index("--name") + 1]
        self.assertIn(["docker", "rm", "-f", container_name], calls)

    @patch(
        "clawpatch_supervise.validation_services._verified_postgres_image",
        return_value="postgres:16",
    )
    @patch("clawpatch_supervise.validation_services._compose_contract")
    def test_startup_timeout_removes_exact_container_name(
        self, compose_contract, _verified_postgres_image
    ) -> None:
        compose_contract.return_value = PostgresTestContract(
            compose_file=Path("compose.yaml"),
            image="postgres:16",
            url_env="TEST_DATABASE_URL",
            reset_envs=("BTT_ALLOW_DATABASE_RESET",),
        )
        calls: list[list[str]] = []

        def run(
            argv: list[str],
            *,
            cwd: Path,
            timeout: int,
            env: Mapping[str, str],
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(argv, timeout)
            if argv[:3] == ["docker", "rm", "-f"]:
                return subprocess.CompletedProcess(argv, 0, "", "")
            self.fail(f"unexpected command: {argv}")

        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(
                SafetyError,
                "Disposable PostgreSQL startup timed out",
            ):
                with _provision_postgres_test_environment(Path(temp), run=run):
                    self.fail("timed out startup must not yield an environment")

        docker_run = next(argv for argv in calls if argv[:2] == ["docker", "run"])
        container_name = docker_run[docker_run.index("--name") + 1]
        self.assertIn(["docker", "rm", "-f", container_name], calls)


if __name__ == "__main__":
    unittest.main()
