from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clawpatch_supervise.errors import SafetyError
from clawpatch_supervise.validation_services import (
    PostgresTestContract,
    _provision_postgres_test_environment,
)


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

        def run(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
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

        def run(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
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

        def run(argv: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
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
