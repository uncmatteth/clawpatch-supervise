from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

from .errors import SafetyError


_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_IGNORED_DIRECTORIES = frozenset(
    {".git", ".clawpatch", ".manageroo", "node_modules", "dist", "build", "target", ".venv", "venv"}
)
_TEST_SUFFIXES = frozenset(
    {".cjs", ".js", ".jsx", ".mjs", ".py", ".rb", ".ts", ".tsx"}
)
_OFFICIAL_POSTGRES_IMAGE = re.compile(
    r"^(?:(?:docker\.io/)?library/)?postgres:"
    r"(?:[1-9][0-9]*(?:\.[0-9]+)?(?:-[A-Za-z0-9_.-]+)?|sha256:[0-9a-f]{64})$"
)
_IMAGE_LINE = re.compile(r"(?m)^\s*image:\s*['\"]?([^\s#'\"]+)['\"]?\s*(?:#.*)?$")
_RESET_ENV = re.compile(r"\b([A-Z][A-Z0-9_]*ALLOW_DATABASE_RESET)\b")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PRODUCTION_ENV = re.compile(r"(?:^|_)(?:LIVE|PROD|PRODUCTION)(?:_|$)")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{12,64}$")
_VALIDATION_CONTRACT_FILE = "manageroo-validation.toml"
_MAX_TEST_SOURCE_BYTES = 24 * 1024 * 1024
_DEFAULT_READY_SECONDS = 90
_MAX_PYTHON_REQUIREMENTS = 256
_MAX_PYTHON_REQUIREMENT_BYTES = 4096
_MAX_PYTHON_REQUIREMENTS_BYTES = 64 * 1024


@dataclass(frozen=True)
class PostgresTestContract:
    compose_file: Path
    image: str
    url_env: str
    reset_envs: tuple[str, ...]


@dataclass(frozen=True)
class PythonTestContract:
    pyproject_file: Path
    requirements: tuple[str, ...]


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
Progress = Callable[[dict[str, object]], None]


def _run_command(
    argv: list[str], *, cwd: Path, timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        timeout=timeout,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )


def _repository_identity(repo: Path) -> str:
    return hashlib.sha256(os.fsencode(str(repo.resolve()))).hexdigest()


def _test_contract_envs(repo: Path) -> tuple[str, tuple[str, ...]] | None:
    total = 0
    reset_envs: set[str] = set()
    found_url = False
    for root, directories, files in os.walk(repo):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in _IGNORED_DIRECTORIES
        )
        root_path = Path(root)
        for name in sorted(files):
            path = root_path / name
            if path.suffix.lower() not in _TEST_SUFFIXES or path.is_symlink():
                continue
            relative = path.relative_to(repo).as_posix().lower()
            if "test" not in relative and "spec" not in relative:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            total += size
            if total > _MAX_TEST_SOURCE_BYTES:
                raise SafetyError(
                    "ClawPatch Supervise refused unbounded disposable-database contract discovery."
                )
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "TEST_DATABASE_URL" not in text:
                continue
            local_reset_envs = set(_RESET_ENV.findall(text))
            if not local_reset_envs:
                continue
            found_url = True
            reset_envs.update(local_reset_envs)
    if not found_url or not reset_envs:
        return None
    contract_file = repo / _VALIDATION_CONTRACT_FILE
    if not contract_file.is_file():
        raise SafetyError(
            "Disposable PostgreSQL validation requires an explicit "
            f"{_VALIDATION_CONTRACT_FILE} reset-variable contract."
        )
    if contract_file.is_symlink() or contract_file.resolve().parent != repo.resolve():
        raise SafetyError("The disposable PostgreSQL validation contract must be a root file.")
    try:
        with contract_file.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SafetyError(
            "ClawPatch Supervise could not read the disposable PostgreSQL validation contract."
        ) from exc
    postgres = payload.get("postgres")
    if not isinstance(postgres, dict):
        raise SafetyError("The disposable PostgreSQL validation contract needs [postgres].")
    url_env = postgres.get("url_env")
    reset_env = postgres.get("reset_env")
    if url_env != "TEST_DATABASE_URL":
        raise SafetyError(
            "The disposable PostgreSQL validation contract must use TEST_DATABASE_URL."
        )
    if (
        not isinstance(reset_env, str)
        or _RESET_ENV.fullmatch(reset_env) is None
        or _ENV_NAME.fullmatch(reset_env) is None
        or _PRODUCTION_ENV.search(reset_env)
    ):
        raise SafetyError(
            "The disposable PostgreSQL validation contract has an unsafe reset variable."
        )
    unexpected = sorted(reset_envs - {reset_env})
    if unexpected:
        raise SafetyError(
            "Test sources contain an unconfigured database reset guard: "
            + ", ".join(unexpected)
        )
    if reset_env not in reset_envs:
        return None
    return url_env, (reset_env,)


def _compose_contract(repo: Path) -> PostgresTestContract | None:
    compose_files = [repo / name for name in _COMPOSE_FILES if (repo / name).is_file()]
    if len(compose_files) != 1:
        return None
    compose_file = compose_files[0]
    if compose_file.is_symlink() or compose_file.resolve().parent != repo.resolve():
        return None
    try:
        raw = compose_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    raw_images = sorted(set(_IMAGE_LINE.findall(raw)))
    postgres_images = [image for image in raw_images if _OFFICIAL_POSTGRES_IMAGE.fullmatch(image)]
    if len(postgres_images) != 1:
        return None
    env_contract = _test_contract_envs(repo)
    if env_contract is None:
        return None
    url_env, reset_envs = env_contract
    return PostgresTestContract(
        compose_file=compose_file,
        image=postgres_images[0],
        url_env=url_env,
        reset_envs=reset_envs,
    )


def _python_test_contract(repo: Path) -> PythonTestContract | None:
    pyproject_file = repo / "pyproject.toml"
    if not pyproject_file.is_file() or pyproject_file.is_symlink():
        return None
    if pyproject_file.resolve().parent != repo.resolve():
        return None
    try:
        with pyproject_file.open("rb") as handle:
            payload = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SafetyError(
            "ClawPatch Supervise could not read this repository's Python validation manifest."
        ) from exc
    tool = payload.get("tool")
    pytest_configured = (
        isinstance(tool, dict) and isinstance(tool.get("pytest"), dict)
    ) or (repo / "pytest.ini").is_file()
    if not pytest_configured:
        return None
    project = payload.get("project")
    if not isinstance(project, dict):
        return None
    requirement_groups: list[object] = [project.get("dependencies", [])]
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise SafetyError("Python project.optional-dependencies must be a table.")
    for name in ("test", "tests", "dev", "development"):
        if name in optional:
            requirement_groups.append(optional[name])
    raw_requirements: list[object] = []
    for group in requirement_groups:
        if not isinstance(group, list):
            raise SafetyError("Python project.dependencies must be a bounded string list.")
        raw_requirements.extend(group)
    if len(raw_requirements) > _MAX_PYTHON_REQUIREMENTS:
        raise SafetyError("ClawPatch Supervise refused unbounded Python dependency discovery.")
    requirements: list[str] = []
    total = 0
    for value in raw_requirements:
        if not isinstance(value, str):
            raise SafetyError("Python project.dependencies must contain only strings.")
        requirement = value.strip()
        encoded_size = len(requirement.encode("utf-8"))
        if (
            not requirement
            or requirement.startswith("-")
            or "\x00" in requirement
            or "\n" in requirement
            or "\r" in requirement
            or encoded_size > _MAX_PYTHON_REQUIREMENT_BYTES
        ):
            raise SafetyError("Python project.dependencies contains an unsafe requirement.")
        total += encoded_size
        if total > _MAX_PYTHON_REQUIREMENTS_BYTES:
            raise SafetyError("ClawPatch Supervise refused unbounded Python dependency discovery.")
        requirements.append(requirement)
    return PythonTestContract(
        pyproject_file=pyproject_file,
        requirements=tuple(requirements),
    )


def _python_environment_bin(environment: Path) -> tuple[Path, Path]:
    executable_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    return executable_dir, python


def _checked_python_environment_command(
    run: RunCommand,
    argv: list[str],
    *,
    repo: Path,
    timeout: int,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = run(argv, cwd=repo, timeout=timeout)
    except (FileNotFoundError, OSError) as exc:
        raise SafetyError(
            f"Disposable Python validation environment {action} could not start."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SafetyError(
            f"Disposable Python validation environment {action} timed out."
        ) from exc
    if result.returncode != 0:
        output = "\n".join(value for value in (result.stdout, result.stderr) if value)
        raise SafetyError(
            f"Disposable Python validation environment {action} failed with exit code "
            f"{result.returncode}: {output[-4000:]}"
        )
    return result


@contextmanager
def _provision_python_test_environment(
    repo: Path,
    contract: PythonTestContract | None,
    *,
    run: RunCommand,
    progress: Progress | None,
) -> Iterator[dict[str, str]]:
    if contract is None:
        yield {}
        return
    if progress is not None:
        progress(
            {
                "phase": "validation-environment-start",
                "current": "?",
                "total": "?",
                "command": "create disposable Python validation environment",
                "attempt": 1,
                "max_attempts": 1,
            }
        )
    try:
        with tempfile.TemporaryDirectory(prefix="manageroo-validation-python-") as temp:
            environment = Path(temp) / "venv"
            _checked_python_environment_command(
                run,
                [sys.executable, "-m", "venv", str(environment)],
                repo=repo,
                timeout=120,
                action="creation",
            )
            executable_dir, python = _python_environment_bin(environment)
            if not python.is_file():
                raise SafetyError(
                    "Disposable Python validation environment did not create its interpreter."
                )
            _checked_python_environment_command(
                run,
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "pytest>=8,<10",
                    "--",
                    *contract.requirements,
                ],
                repo=repo,
                timeout=900,
                action="dependency installation",
            )
            _checked_python_environment_command(
                run,
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--no-input",
                    "--no-deps",
                    "--",
                    ".",
                ],
                repo=repo,
                timeout=900,
                action="project installation",
            )
            child_path = str(executable_dir)
            inherited_path = os.environ.get("PATH")
            if inherited_path:
                child_path += os.pathsep + inherited_path
            child_env = {
                "PATH": child_path,
                "VIRTUAL_ENV": str(environment),
                "PYTHONNOUSERSITE": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
            }
            if progress is not None:
                progress(
                    {
                        "phase": "validation-environment-ready",
                        "current": "?",
                        "total": "?",
                        "detail": "disposable Python validation environment ready",
                    }
                )
            yield child_env
    finally:
        if progress is not None:
            progress(
                {
                    "phase": "validation-environment-cleanup",
                    "current": "?",
                    "total": "?",
                    "detail": "disposable Python validation environment removed",
                }
            )


def _checked(
    run: RunCommand,
    argv: list[str],
    *,
    repo: Path,
    timeout: int,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = run(argv, cwd=repo, timeout=timeout)
    except (FileNotFoundError, OSError) as exc:
        raise SafetyError(
            "This repository requires Docker to create its disposable PostgreSQL "
            "validation database. Install and start Docker, then resume the stopped finding."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SafetyError(f"Disposable PostgreSQL {action} timed out.") from exc
    if result.returncode != 0:
        output = "\n".join(value for value in (result.stdout, result.stderr) if value)
        raise SafetyError(
            f"Disposable PostgreSQL {action} failed with exit code {result.returncode}: "
            f"{output[-2000:]}"
        )
    return result


def _verified_postgres_image(
    repo: Path,
    contract: PostgresTestContract,
    *,
    run: RunCommand,
) -> str:
    result = _checked(
        run,
        ["docker", "compose", "config", "--format", "json"],
        repo=repo,
        timeout=60,
        action="compose inspection",
    )
    try:
        payload = json.loads(result.stdout)
        services = payload["services"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SafetyError("Docker Compose did not return a valid service definition.") from exc
    images = sorted(
        {
            str(service.get("image", ""))
            for service in services.values()
            if isinstance(service, dict)
            and _OFFICIAL_POSTGRES_IMAGE.fullmatch(str(service.get("image", "")))
        }
    )
    if images != [contract.image]:
        raise SafetyError(
            "The resolved Docker Compose PostgreSQL image does not match the exact "
            "official versioned image declared by the repository."
        )
    return contract.image


def _published_port(
    repo: Path,
    container_id: str,
    *,
    run: RunCommand,
) -> int:
    result = _checked(
        run,
        ["docker", "port", container_id, "5432/tcp"],
        repo=repo,
        timeout=30,
        action="port inspection",
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    loopback = [line for line in lines if line.startswith("127.0.0.1:")]
    if len(loopback) != 1:
        raise SafetyError("Disposable PostgreSQL did not publish exactly one loopback port.")
    try:
        port = int(loopback[0].rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise SafetyError("Disposable PostgreSQL returned an invalid loopback port.") from exc
    if not 1 <= port <= 65535:
        raise SafetyError("Disposable PostgreSQL returned an out-of-range loopback port.")
    return port


def _remove_container(
    repo: Path,
    container_id: str,
    *,
    run: RunCommand,
) -> None:
    try:
        result = run(["docker", "rm", "-f", container_id], cwd=repo, timeout=60)
    except (FileNotFoundError, OSError) as exc:
        raise SafetyError(
            "ClawPatch Supervise could not remove its disposable PostgreSQL container because "
            "Docker is unavailable."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SafetyError(
            "ClawPatch Supervise timed out removing its disposable PostgreSQL container."
        ) from exc
    if result.returncode != 0:
        output = "\n".join(value for value in (result.stdout, result.stderr) if value)
        raise SafetyError(
            "ClawPatch Supervise could not remove its disposable PostgreSQL container: "
            + output[-2000:]
        )


@contextmanager
def _provision_postgres_test_environment(
    repo: Path,
    *,
    run: RunCommand = _run_command,
    progress: Progress | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    password_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
) -> Iterator[dict[str, str]]:
    root = repo.expanduser().resolve()
    contract = _compose_contract(root)
    if contract is None:
        yield {}
        return
    if os.environ.get(contract.url_env) and all(
        os.environ.get(name) == "true" for name in contract.reset_envs
    ):
        yield {}
        return

    if progress is not None:
        progress(
            {
                "phase": "validation-service-start",
                "current": "?",
                "total": "?",
                "command": "create owned disposable PostgreSQL validation database",
                "attempt": 1,
                "max_attempts": 1,
            }
        )
    image = _verified_postgres_image(root, contract, run=run)
    password = password_factory()
    if not password or "\x00" in password:
        raise SafetyError("Disposable PostgreSQL generated an invalid password.")
    repository_identity = _repository_identity(root)
    validation_run_identity = secrets.token_hex(16)
    container_name = (
        f"manageroo-validation-postgres-{repository_identity[:16]}-"
        f"{validation_run_identity}"
    )
    result = _checked(
        run,
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--name",
            container_name,
            "--label",
            "manageroo.validation-service=postgresql",
            "--label",
            f"manageroo.repository={repository_identity}",
            "--label",
            f"manageroo.validation-run={validation_run_identity}",
            "--mount",
            "type=tmpfs,destination=/var/lib/postgresql/data",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_DB=manageroo_test",
            "--env",
            "POSTGRES_USER=manageroo",
            "--env",
            f"POSTGRES_PASSWORD={password}",
            "--pull=missing",
            image,
        ],
        repo=root,
        timeout=180,
        action="startup",
    )
    container_id = result.stdout.strip()
    valid_container_id = _CONTAINER_ID.fullmatch(container_id) is not None
    body_error: BaseException | None = None
    try:
        if not valid_container_id:
            raise SafetyError("Docker returned an invalid disposable PostgreSQL container ID.")
        port = _published_port(root, container_id, run=run)
        deadline = monotonic() + _DEFAULT_READY_SECONDS
        while True:
            try:
                ready = run(
                    [
                        "docker",
                        "exec",
                        container_id,
                        "pg_isready",
                        "-U",
                        "manageroo",
                        "-d",
                        "manageroo_test",
                    ],
                    cwd=root,
                    timeout=15,
                )
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                ready = subprocess.CompletedProcess([], 1, "", "not ready")
            if ready.returncode == 0:
                break
            if monotonic() >= deadline:
                raise SafetyError(
                    "Disposable PostgreSQL did not become healthy within 90 seconds."
                )
            sleep(1)
        child_env = {
            contract.url_env: (
                "postgresql://manageroo:"
                + quote(password, safe="")
                + f"@127.0.0.1:{port}/manageroo_test"
            ),
            **{name: "true" for name in contract.reset_envs},
        }
        if progress is not None:
            progress(
                {
                    "phase": "validation-service-ready",
                    "current": "?",
                    "total": "?",
                    "detail": "owned disposable PostgreSQL validation database ready",
                }
            )
        yield child_env
    except BaseException as exc:
        body_error = exc
        raise
    finally:
        if valid_container_id:
            try:
                _remove_container(root, container_id, run=run)
            except SafetyError as cleanup_error:
                if body_error is None:
                    raise
                body_error.add_note(
                    f"Disposable PostgreSQL cleanup also failed: {cleanup_error}"
                )
            else:
                if progress is not None:
                    progress(
                        {
                            "phase": "validation-service-cleanup",
                            "current": "?",
                            "total": "?",
                            "detail": "owned disposable PostgreSQL validation database removed",
                        }
                    )


@contextmanager
def provision_disposable_validation_environment(
    repo: Path,
    *,
    run: RunCommand = _run_command,
    progress: Progress | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    password_factory: Callable[[], str] = lambda: secrets.token_urlsafe(24),
) -> Iterator[dict[str, str]]:
    root = repo.expanduser().resolve()
    python_contract = _python_test_contract(root)
    with _provision_python_test_environment(
        root,
        python_contract,
        run=run,
        progress=progress,
    ) as python_env:
        with _provision_postgres_test_environment(
            root,
            run=run,
            progress=progress,
            sleep=sleep,
            monotonic=monotonic,
            password_factory=password_factory,
        ) as postgres_env:
            yield {**python_env, **postgres_env}
