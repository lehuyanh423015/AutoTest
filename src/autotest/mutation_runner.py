"""External WSL/Mutmut mutation evaluation for the final accepted test suite."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from autotest.errors import MutationEnvironmentUnavailableError, MutationError
from autotest.project_analyzer import FunctionInfo

MUTMUT_VERSION = "3.7.0"
PYTEST_VERSION = "8.4.2"
DEFAULT_MUTATION_VENV = "/home/ubuntu/autotest-mutation-env"
ENVIRONMENT_CHECK_TIMEOUT = 30.0
_STAT_FIELDS = (
    "killed",
    "survived",
    "no_tests",
    "skipped",
    "suspicious",
    "timeout",
    "check_was_interrupted_by_user",
    "segfault",
)


class MutationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    NO_MUTANTS = "NO_MUTANTS"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    TOOL_ERROR = "TOOL_ERROR"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class MutationResult:
    target_file: Path
    function_name: str
    status: MutationStatus
    total_mutants: int = 0
    killed_mutants: int = 0
    survived_mutants: int = 0
    no_tests_mutants: int = 0
    skipped_mutants: int = 0
    suspicious_mutants: int = 0
    timeout_mutants: int = 0
    interrupted_mutants: int = 0
    segfault_mutants: int = 0
    unreported_mutants: int = 0
    tool_total_mutants: int = 0
    mutation_score_percent: float | None = None
    duration_seconds: float = 0.0
    backend: str = "mutmut"
    backend_version: str | None = None
    python_version: str | None = None
    pytest_version: str | None = None
    mutation_venv: str | None = None
    raw_stats_file: Path | None = None
    workspace: Path | None = None
    hashes: Mapping[str, Any] = field(default_factory=dict)
    unknown_categories: Mapping[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    stdout: str = field(default="", repr=False)
    stderr: str = field(default="", repr=False)


class MutationBackend(ABC):
    """Small boundary for replaceable external mutation engines."""

    @abstractmethod
    def run(
        self,
        function: FunctionInfo,
        test_files: Sequence[Path | str],
        mutation_dir: Path | str,
    ) -> MutationResult: ...


def calculate_mutation_score(killed: int, survived: int) -> float | None:
    """Return killed / (killed + survived) * 100, or N/A for no denominator."""
    if (
        isinstance(killed, bool)
        or isinstance(survived, bool)
        or not isinstance(killed, int)
        or not isinstance(survived, int)
        or killed < 0
        or survived < 0
    ):
        raise ValueError("Mutation counts must be non-negative integers.")
    denominator = killed + survived
    return None if denominator == 0 else killed / denominator * 100.0


def parse_mutmut_stats(
    raw: str | bytes,
    *,
    target_file: Path | str,
    function_name: str,
    duration_seconds: float = 0.0,
    backend_version: str = MUTMUT_VERSION,
    raw_stats_file: Path | None = None,
    workspace: Path | None = None,
    python_version: str | None = None,
    pytest_version: str | None = None,
    mutation_venv: str | None = None,
    hashes: Mapping[str, Any] | None = None,
) -> MutationResult:
    """Normalize the supported ``mutmut export-cicd-stats`` v3.7.0 schema."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise MutationError(f"Malformed Mutmut statistics JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise MutationError("Mutmut statistics must be a JSON object.")
    required = ("total", *_STAT_FIELDS)
    missing = [name for name in required if name not in payload]
    if missing:
        raise MutationError(f"Mutmut statistics missing required fields: {', '.join(missing)}")
    for name in required:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise MutationError(f"Mutmut statistic {name!r} must be a non-negative integer.")
    selected_total = sum(payload[name] for name in _STAT_FIELDS)
    if selected_total > payload["total"]:
        raise MutationError("Mutmut category counts exceed its total mutant count.")
    unknown = {key: value for key, value in payload.items() if key not in required}
    status = MutationStatus.NO_MUTANTS if selected_total == 0 else MutationStatus.COMPLETE
    return MutationResult(
        target_file=Path(target_file).resolve(),
        function_name=function_name,
        status=status,
        total_mutants=selected_total,
        killed_mutants=payload["killed"],
        survived_mutants=payload["survived"],
        no_tests_mutants=payload["no_tests"],
        skipped_mutants=payload["skipped"],
        suspicious_mutants=payload["suspicious"],
        timeout_mutants=payload["timeout"],
        interrupted_mutants=payload["check_was_interrupted_by_user"],
        segfault_mutants=payload["segfault"],
        unreported_mutants=payload["total"] - selected_total,
        tool_total_mutants=payload["total"],
        mutation_score_percent=calculate_mutation_score(payload["killed"], payload["survived"]),
        duration_seconds=duration_seconds,
        backend_version=backend_version,
        python_version=python_version,
        pytest_version=pytest_version,
        mutation_venv=mutation_venv,
        raw_stats_file=raw_stats_file,
        workspace=workspace,
        hashes=dict(hashes or {}),
        unknown_categories=unknown,
    )


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class WSLMutmutBackend(MutationBackend):
    """Run pinned Mutmut through WSL in a fresh copied workspace."""

    def __init__(
        self,
        *,
        distro: str = "Ubuntu-22.04",
        timeout: float = 300.0,
        expected_version: str = MUTMUT_VERSION,
        expected_pytest_version: str = PYTEST_VERSION,
        mutation_venv: str = DEFAULT_MUTATION_VENV,
        process_runner: ProcessRunner = subprocess.run,
    ) -> None:
        if timeout <= 0:
            raise ValueError("Mutation timeout must be greater than zero.")
        self.distro = distro
        self.timeout = float(timeout)
        self.expected_version = expected_version
        self.expected_pytest_version = expected_pytest_version
        venv = PurePosixPath(mutation_venv)
        if not venv.is_absolute():
            raise ValueError("Mutation virtualenv must be an absolute WSL path.")
        self.mutation_venv = str(venv)
        self.python_executable = str(venv / "bin" / "python")
        self.pytest_executable = str(venv / "bin" / "pytest")
        self.mutmut_executable = str(venv / "bin" / "mutmut")
        self._process_runner = process_runner

    def check_environment(self) -> tuple[str, str, str]:
        """Return Mutmut, Python, and pytest versions or raise a clear availability error."""
        return self._check_environment(lambda: ENVIRONMENT_CHECK_TIMEOUT)

    def _check_environment(self, timeout: Callable[[], float]) -> tuple[str, str, str]:
        try:
            probe = self._run_wsl(("/bin/sh", "-c", "printf WSL_READY"), timeout())
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise MutationEnvironmentUnavailableError(f"WSL is unavailable: {exc}") from exc
        if probe.returncode != 0 or probe.stdout.strip() != "WSL_READY":
            raise MutationEnvironmentUnavailableError(
                f"WSL distribution {self.distro!r} is unavailable: {probe.stderr.strip()}"
            )
        python = self._version((self.python_executable, "--version"), "Python", timeout())
        pytest_text = self._version((self.pytest_executable, "--version"), "pytest", timeout())
        pytest_actual = self._semantic_version(pytest_text, "pytest")
        if pytest_actual != self.expected_pytest_version:
            raise MutationEnvironmentUnavailableError(
                "pytest version mismatch: "
                f"expected {self.expected_pytest_version}, found {pytest_actual}."
            )
        mutmut_text = self._version((self.mutmut_executable, "--version"), "Mutmut", timeout())
        actual = self._semantic_version(mutmut_text, "Mutmut")
        if actual != self.expected_version:
            raise MutationEnvironmentUnavailableError(
                f"Mutmut version mismatch: expected {self.expected_version}, found {actual}."
            )
        return actual, python, pytest_text

    def run(
        self,
        function: FunctionInfo,
        test_files: Sequence[Path | str],
        mutation_dir: Path | str,
    ) -> MutationResult:
        started = time.perf_counter()
        root = Path(mutation_dir).resolve()
        workspace = root / "workspace"
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        hashes: dict[str, Any] = {}
        protected: dict[str, str] = {}
        version = python_version = pytest_version = None
        try:
            version, python_version, pytest_version = self._check_environment(
                lambda: min(ENVIRONMENT_CHECK_TIMEOUT, self._remaining(started))
            )
            copied_source, copied_tests, config = self._prepare_workspace(
                function, test_files, workspace
            )
            original_paths = (
                function.file_path.resolve(),
                *(Path(item).resolve() for item in test_files),
            )
            protected = {str(path): _sha256(path) for path in original_paths}
            hashes = {
                "target_source_sha256": protected[str(function.file_path.resolve())],
                "accepted_test_sha256": {
                    str(path): protected[str(path)] for path in original_paths[1:]
                },
                "mutation_config_sha256": _sha256(config),
                "workspace_source_sha256": _sha256(copied_source),
                "workspace_test_sha256": {path.name: _sha256(path) for path in copied_tests},
            }
            if hashes["workspace_source_sha256"] != hashes["target_source_sha256"]:
                raise MutationError("Copied mutation source does not match the protected target.")
            copied_test_hashes = tuple(hashes["workspace_test_sha256"].values())
            original_test_hashes = tuple(hashes["accepted_test_sha256"].values())
            if copied_test_hashes != original_test_hashes:
                raise MutationError("Copied mutation tests do not match the accepted suite.")
            linux_workspace = self._linux_path(workspace, self._remaining(started))
            baseline = self._run_in_workspace(
                linux_workspace,
                (self.pytest_executable, "tests", "-q", "--disable-warnings"),
                self._remaining(started),
            )
            stdout_parts.append(f"[clean pytest]\n{baseline.stdout}")
            stderr_parts.append(f"[clean pytest]\n{baseline.stderr}")
            if baseline.returncode != 0:
                raise MutationError("Clean pytest baseline failed inside the mutation workspace.")
            selector = f"{function.module_name}.x_{function.function_name}__mutmut_*"
            mutated = self._run_in_workspace(
                linux_workspace,
                (self.mutmut_executable, "run", "--max-children", "1", selector),
                self._remaining(started),
            )
            stdout_parts.append(f"[mutmut run]\n{mutated.stdout}")
            stderr_parts.append(f"[mutmut run]\n{mutated.stderr}")
            if mutated.returncode != 0:
                output = f"{mutated.stdout}\n{mutated.stderr}"
                if "Filtered for specific mutants, but nothing matches" in output:
                    self._verify_integrity(protected)
                    return MutationResult(
                        function.file_path.resolve(),
                        function.function_name,
                        MutationStatus.NO_MUTANTS,
                        duration_seconds=time.perf_counter() - started,
                        backend_version=version,
                        python_version=python_version,
                        pytest_version=pytest_version,
                        mutation_venv=self.mutation_venv,
                        workspace=workspace,
                        hashes=hashes,
                        stdout="\n".join(stdout_parts),
                        stderr="\n".join(stderr_parts),
                    )
                raise MutationError(f"Mutmut run failed with exit code {mutated.returncode}.")
            exported = self._run_in_workspace(
                linux_workspace,
                (self.mutmut_executable, "export-cicd-stats"),
                self._remaining(started),
            )
            stdout_parts.append(f"[mutmut export-cicd-stats]\n{exported.stdout}")
            stderr_parts.append(f"[mutmut export-cicd-stats]\n{exported.stderr}")
            if exported.returncode != 0:
                raise MutationError("Mutmut statistics export failed.")
            workspace_raw = workspace / "mutants" / "mutmut-cicd-stats.json"
            raw_file = root / "mutmut_raw_stats.json"
            try:
                raw_bytes = workspace_raw.read_bytes()
                with raw_file.open("xb") as destination:
                    destination.write(raw_bytes)
            except (OSError, FileExistsError) as exc:
                raise MutationError(f"Could not preserve Mutmut raw statistics: {exc}") from exc
            self._verify_integrity(protected)
            normalized = parse_mutmut_stats(
                raw_bytes,
                target_file=function.file_path,
                function_name=function.function_name,
                duration_seconds=time.perf_counter() - started,
                backend_version=version,
                raw_stats_file=raw_file,
                workspace=workspace,
                python_version=python_version,
                pytest_version=pytest_version,
                mutation_venv=self.mutation_venv,
                hashes=hashes,
            )
            return replace(
                normalized,
                stdout="\n".join(stdout_parts),
                stderr="\n".join(stderr_parts),
            )
        except subprocess.TimeoutExpired as exc:
            if exc.stdout:
                stdout_parts.append(str(exc.stdout))
            if exc.stderr:
                stderr_parts.append(str(exc.stderr))
            return self._failure(
                function,
                MutationStatus.TIMEOUT,
                f"Mutation timeout exceeded: {exc}",
                started,
                root,
                workspace,
                version,
                python_version,
                pytest_version,
                hashes,
                stdout_parts,
                stderr_parts,
                protected,
            )
        except MutationEnvironmentUnavailableError as exc:
            return self._failure(
                function,
                MutationStatus.TOOL_UNAVAILABLE,
                str(exc),
                started,
                root,
                workspace,
                version,
                python_version,
                pytest_version,
                hashes,
                stdout_parts,
                stderr_parts,
                protected,
            )
        except (MutationError, OSError) as exc:
            return self._failure(
                function,
                MutationStatus.TOOL_ERROR,
                str(exc),
                started,
                root,
                workspace,
                version,
                python_version,
                pytest_version,
                hashes,
                stdout_parts,
                stderr_parts,
                protected,
            )

    def _prepare_workspace(
        self, function: FunctionInfo, test_files: Sequence[Path | str], workspace: Path
    ) -> tuple[Path, tuple[Path, ...], Path]:
        if not test_files:
            raise MutationError("Mutation evaluation requires at least one accepted test file.")
        tests_dir = workspace / "tests"
        try:
            workspace.mkdir(parents=True, exist_ok=False)
            tests_dir.mkdir()
            copied_source = workspace / function.file_path.name
            shutil.copyfile(function.file_path, copied_source)
            copied_tests = []
            for index, item in enumerate(test_files):
                source = Path(item).resolve()
                destination = tests_dir / f"test_accepted_{index:03d}.py"
                shutil.copyfile(source, destination)
                copied_tests.append(destination)
            config = workspace / "pyproject.toml"
            config.write_text(
                "[tool.mutmut]\n"
                f'source_paths = ["{function.file_path.name}"]\n'
                'pytest_add_cli_args_test_selection = ["tests"]\n'
                "use_git_change_detection = false\n\n"
                "[tool.pytest.ini_options]\n"
                'pythonpath = ["."]\n'
                'addopts = ["--import-mode=importlib", "-p", "no:cacheprovider"]\n',
                encoding="utf-8",
                newline="",
            )
            config_dir = workspace.parent / "config"
            config_dir.mkdir(exist_ok=True)
            with (config_dir / "pyproject.toml").open("xb") as destination:
                destination.write(config.read_bytes())
        except OSError as exc:
            raise MutationError(f"Could not prepare fresh mutation workspace: {exc}") from exc
        return copied_source, tuple(copied_tests), config

    def _version(self, command: tuple[str, ...], label: str, timeout: float) -> str:
        try:
            completed = self._run_wsl(command, timeout)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise MutationEnvironmentUnavailableError(
                f"{label} is unavailable in WSL: {exc}"
            ) from exc
        output = (completed.stdout or completed.stderr).strip()
        if completed.returncode != 0 or not output:
            raise MutationEnvironmentUnavailableError(
                f"{label} is unavailable in WSL: {completed.stderr.strip()}"
            )
        return output

    @staticmethod
    def _semantic_version(output: str, label: str) -> str:
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output)
        if match is None:
            raise MutationEnvironmentUnavailableError(
                f"Could not parse {label} semantic version from: {output!r}."
            )
        return match.group(1)

    def _linux_path(self, path: Path, timeout: float) -> str:
        completed = self._run_wsl(("/usr/bin/wslpath", "-a", "-u", str(path)), timeout)
        if completed.returncode != 0 or not completed.stdout.strip():
            raise MutationError(f"Could not translate mutation workspace path: {completed.stderr}")
        return completed.stdout.strip()

    def _run_wsl(
        self, command: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._process_runner(
            ["wsl.exe", "-d", self.distro, "--exec", *command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def _run_in_workspace(
        self, linux_workspace: str, command: tuple[str, ...], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._run_wsl(
            (
                "/bin/sh",
                "-lc",
                'cd "$1" && shift && exec "$@"',
                "autotest-mutation",
                linux_workspace,
                *command,
            ),
            timeout,
        )

    def _remaining(self, started: float) -> float:
        remaining = self.timeout - (time.perf_counter() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired("mutation session", self.timeout)
        return remaining

    @staticmethod
    def _verify_integrity(before: Mapping[str, str]) -> None:
        for raw_path, expected in before.items():
            path = Path(raw_path)
            if _sha256(path) != expected:
                raise MutationError(f"Mutation evaluation changed protected input: {path}")

    def _failure(
        self,
        function: FunctionInfo,
        status: MutationStatus,
        message: str,
        started: float,
        root: Path,
        workspace: Path,
        version: str | None,
        python_version: str | None,
        pytest_version: str | None,
        hashes: Mapping[str, Any],
        stdout_parts: Sequence[str],
        stderr_parts: Sequence[str],
        protected: Mapping[str, str],
    ) -> MutationResult:
        try:
            WSLMutmutBackend._verify_integrity(protected)
        except MutationError as exc:
            status = MutationStatus.TOOL_ERROR
            message = str(exc)
        return MutationResult(
            function.file_path.resolve(),
            function.function_name,
            status,
            duration_seconds=time.perf_counter() - started,
            backend_version=version,
            python_version=python_version,
            pytest_version=pytest_version,
            mutation_venv=self.mutation_venv,
            workspace=workspace if workspace.exists() else None,
            hashes=dict(hashes),
            error_message=message,
            stdout="\n".join(stdout_parts),
            stderr="\n".join(stderr_parts),
        )


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise MutationError(f"Could not hash mutation input {path}: {exc}") from exc
