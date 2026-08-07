from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

        def run(
            argv: list[str], *, cwd: Path, timeout: int
        ) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:2] == ["docker", "run"]:
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


if __name__ == "__main__":
    unittest.main()
