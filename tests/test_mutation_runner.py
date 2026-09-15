import json
import subprocess
from pathlib import Path

import pytest

from autotest.errors import MutationError
from autotest.mutation_runner import (
    DEFAULT_MUTATION_VENV,
    MUTMUT_VERSION,
    PYTEST_VERSION,
    MutationStatus,
    WSLMutmutBackend,
    calculate_mutation_score,
    parse_mutmut_stats,
)
from autotest.project_analyzer import FunctionInfo


def stats(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "killed": 3,
        "survived": 1,
        "total": 10,
        "no_tests": 1,
        "skipped": 1,
        "suspicious": 1,
        "timeout": 1,
        "check_was_interrupted_by_user": 0,
        "segfault": 0,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("killed", "survived", "expected"),
    [(4, 0, 100.0), (2, 2, 50.0), (0, 4, 0.0), (0, 0, None)],
)
def test_mutation_score_formula(killed: int, survived: int, expected: float | None) -> None:
    assert calculate_mutation_score(killed, survived) == expected


def test_parser_normalizes_supported_categories_and_excludes_them_from_score(
    tmp_path: Path,
) -> None:
    result = parse_mutmut_stats(
        json.dumps(stats()), target_file=tmp_path / "target.py", function_name="target"
    )

    assert result.status is MutationStatus.COMPLETE
    assert result.total_mutants == 8
    assert result.tool_total_mutants == 10
    assert result.unreported_mutants == 2
    assert result.mutation_score_percent == 75.0
    assert result.no_tests_mutants == 1
    assert result.skipped_mutants == 1
    assert result.suspicious_mutants == 1
    assert result.timeout_mutants == 1


def test_parser_zero_selected_mutants_is_not_100_percent(tmp_path: Path) -> None:
    result = parse_mutmut_stats(
        json.dumps(
            stats(killed=0, survived=0, total=9, no_tests=0, skipped=0, suspicious=0, timeout=0)
        ),
        target_file=tmp_path / "target.py",
        function_name="target",
    )

    assert result.status is MutationStatus.NO_MUTANTS
    assert result.total_mutants == 0
    assert result.unreported_mutants == 9
    assert result.mutation_score_percent is None


def test_parser_preserves_unknown_optional_fields(tmp_path: Path) -> None:
    result = parse_mutmut_stats(
        json.dumps(stats(experimental_category={"count": 2})),
        target_file=tmp_path / "target.py",
        function_name="target",
    )

    assert result.unknown_categories == {"experimental_category": {"count": 2}}


@pytest.mark.parametrize(
    ("killed", "survived", "expected"),
    [(5, 0, 100.0), (0, 5, 0.0), (2, 3, 40.0)],
)
def test_parser_killed_and_survived_outcomes(
    tmp_path: Path, killed: int, survived: int, expected: float
) -> None:
    result = parse_mutmut_stats(
        json.dumps(
            stats(
                killed=killed,
                survived=survived,
                total=5,
                no_tests=0,
                skipped=0,
                suspicious=0,
                timeout=0,
            )
        ),
        target_file=tmp_path / "target.py",
        function_name="target",
    )

    assert result.mutation_score_percent == expected


@pytest.mark.parametrize(
    "payload",
    ["not-json", "[]", json.dumps({"total": 1}), json.dumps(stats(killed=-1))],
)
def test_parser_rejects_malformed_or_incomplete_stats(tmp_path: Path, payload: str) -> None:
    with pytest.raises(MutationError):
        parse_mutmut_stats(payload, target_file=tmp_path / "target.py", function_name="target")


def make_function(tmp_path: Path) -> tuple[FunctionInfo, Path]:
    source = tmp_path / "sample.py"
    source.write_text(
        "def target(value):\n    return value + 1\n\ndef other(value):\n    return value - 1\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "accepted.py"
    test_file.write_text(
        "from sample import target\n\ndef test_target():\n    assert target(1) == 2\n",
        encoding="utf-8",
    )
    return (
        FunctionInfo(
            source.resolve(),
            "sample",
            "target",
            "def target(value):\n    return value + 1",
            1,
            2,
            None,
        ),
        test_file,
    )


class FakeRunner:
    def __init__(
        self,
        mutation_dir: Path,
        *,
        mode: str = "success",
        mutation_venv: str = DEFAULT_MUTATION_VENV,
        protected_source: Path | None = None,
        protected_test: Path | None = None,
    ) -> None:
        self.mutation_dir = mutation_dir
        self.mode = mode
        self.mutation_venv = mutation_venv
        self.protected_source = protected_source
        self.protected_test = protected_test
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        tail = command[4:]
        if self.mode == "wsl-unavailable":
            raise FileNotFoundError("wsl.exe")
        python = f"{self.mutation_venv}/bin/python"
        pytest_path = f"{self.mutation_venv}/bin/pytest"
        mutmut = f"{self.mutation_venv}/bin/mutmut"
        if tail == ["/bin/sh", "-c", "printf WSL_READY"]:
            return subprocess.CompletedProcess(command, 0, "WSL_READY\n", "")
        if tail == [python, "--version"]:
            if self.mode == "python-unavailable":
                return subprocess.CompletedProcess(command, 127, "", f"{python}: not found")
            return subprocess.CompletedProcess(command, 0, "Python 3.10.12\n", "")
        if tail == [pytest_path, "--version"]:
            if self.mode == "pytest-unavailable":
                return subprocess.CompletedProcess(command, 127, "", f"{pytest_path}: not found")
            version = "8.5.0" if self.mode == "wrong-pytest-version" else PYTEST_VERSION
            return subprocess.CompletedProcess(command, 0, f"pytest {version}\n", "")
        if tail == [mutmut, "--version"]:
            if self.mode == "mutmut-unavailable":
                return subprocess.CompletedProcess(command, 127, "", f"{mutmut}: not found")
            version = "3.8.0" if self.mode == "wrong-version" else MUTMUT_VERSION
            return subprocess.CompletedProcess(command, 0, f"mutmut, version {version}\n", "")
        if tail[:3] == ["/usr/bin/wslpath", "-a", "-u"]:
            return subprocess.CompletedProcess(command, 0, "/mnt/e/mutation\n", "")
        if pytest_path in tail:
            return subprocess.CompletedProcess(
                command, 1 if self.mode == "baseline-fail" else 0, "baseline", ""
            )
        if mutmut in tail and "run" in tail:
            if self.mode == "timeout":
                raise subprocess.TimeoutExpired(command, 1)
            if self.mode == "no-mutants":
                return subprocess.CompletedProcess(
                    command, 1, "", "Filtered for specific mutants, but nothing matches"
                )
            if self.mode == "source-mutated" and self.protected_source is not None:
                self.protected_source.write_text(
                    "def target():\n    return 999\n", encoding="utf-8"
                )
            if self.mode == "test-mutated" and self.protected_test is not None:
                self.protected_test.write_text("assert False\n", encoding="utf-8")
            return subprocess.CompletedProcess(
                command, 2 if self.mode == "mutation-fail" else 0, "mutation", ""
            )
        if mutmut in tail and "export-cicd-stats" in tail:
            raw = self.mutation_dir / "workspace" / "mutants" / "mutmut-cicd-stats.json"
            raw.parent.mkdir()
            raw.write_text(json.dumps(stats()), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "exported", "")
        raise AssertionError(f"Unexpected command: {command}")


def run_backend(tmp_path: Path, mode: str = "success", mutation_venv: str = DEFAULT_MUTATION_VENV):
    function, test_file = make_function(tmp_path)
    mutation_dir = tmp_path / "run" / "mutation"
    mutation_dir.mkdir(parents=True)
    fake = FakeRunner(
        mutation_dir,
        mode=mode,
        mutation_venv=mutation_venv,
        protected_source=function.file_path,
        protected_test=test_file,
    )
    result = WSLMutmutBackend(process_runner=fake, mutation_venv=mutation_venv).run(
        function, [test_file], mutation_dir
    )
    return result, fake, function, test_file, mutation_dir


def test_wsl_backend_uses_fresh_workspace_final_tests_and_function_selector(tmp_path: Path) -> None:
    result, fake, function, test_file, mutation_dir = run_backend(tmp_path)

    assert result.status is MutationStatus.COMPLETE
    assert result.backend_version == MUTMUT_VERSION
    assert result.python_version == "Python 3.10.12"
    assert result.pytest_version == "pytest 8.4.2"
    assert result.mutation_venv == DEFAULT_MUTATION_VENV
    assert result.mutation_score_percent == 75.0
    workspace = mutation_dir / "workspace"
    assert (workspace / "sample.py").read_bytes() == function.file_path.read_bytes()
    assert (workspace / "tests" / "test_accepted_000.py").read_bytes() == test_file.read_bytes()
    assert not (workspace / "tests" / "test_accepted_001.py").exists()
    flattened = [part for command in fake.commands for part in command]
    assert f"{DEFAULT_MUTATION_VENV}/bin/python" in flattened
    assert f"{DEFAULT_MUTATION_VENV}/bin/pytest" in flattened
    assert f"{DEFAULT_MUTATION_VENV}/bin/mutmut" in flattened
    assert "python3" not in flattened
    assert "pytest" not in flattened
    assert "mutmut" not in flattened
    assert "sample.x_target__mutmut_*" in flattened
    assert "sample.x_other__mutmut_*" not in flattened
    assert (mutation_dir / "mutmut_raw_stats.json").read_text(encoding="utf-8") == json.dumps(
        stats()
    )
    assert (mutation_dir / "config" / "pyproject.toml").is_file()
    assert function.file_path.read_text(encoding="utf-8").startswith("def target")


@pytest.mark.parametrize(
    ("mode", "status", "message"),
    [
        ("wsl-unavailable", MutationStatus.TOOL_UNAVAILABLE, "WSL is unavailable"),
        ("mutmut-unavailable", MutationStatus.TOOL_UNAVAILABLE, "Mutmut is unavailable"),
        ("python-unavailable", MutationStatus.TOOL_UNAVAILABLE, "Python is unavailable"),
        ("pytest-unavailable", MutationStatus.TOOL_UNAVAILABLE, "pytest is unavailable"),
        ("wrong-pytest-version", MutationStatus.TOOL_UNAVAILABLE, "pytest version mismatch"),
        ("wrong-version", MutationStatus.TOOL_UNAVAILABLE, "version mismatch"),
        ("baseline-fail", MutationStatus.TOOL_ERROR, "baseline failed"),
        ("mutation-fail", MutationStatus.TOOL_ERROR, "Mutmut run failed"),
        ("timeout", MutationStatus.TIMEOUT, "timeout exceeded"),
        ("source-mutated", MutationStatus.TOOL_ERROR, "changed protected input"),
        ("test-mutated", MutationStatus.TOOL_ERROR, "changed protected input"),
    ],
)
def test_wsl_backend_failure_statuses(
    tmp_path: Path, mode: str, status: MutationStatus, message: str
) -> None:
    result, _, _, _, _ = run_backend(tmp_path, mode)

    assert result.status is status
    assert message.lower() in (result.error_message or "").lower()


def test_function_isolation_ignores_unselected_not_checked_mutants(tmp_path: Path) -> None:
    result = parse_mutmut_stats(
        json.dumps(
            stats(killed=2, survived=1, total=25, no_tests=0, skipped=0, suspicious=0, timeout=0)
        ),
        target_file=tmp_path / "two_functions.py",
        function_name="target",
    )

    assert result.total_mutants == 3
    assert result.unreported_mutants == 22
    assert result.mutation_score_percent == pytest.approx(66.6666667)


def test_backend_reports_no_applicable_selected_function_mutants(tmp_path: Path) -> None:
    result, _, _, _, mutation_dir = run_backend(tmp_path, "no-mutants")

    assert result.status is MutationStatus.NO_MUTANTS
    assert result.total_mutants == 0
    assert result.mutation_score_percent is None
    assert not (mutation_dir / "mutmut_raw_stats.json").exists()


def test_backend_derives_absolute_executables_from_configured_virtualenv(tmp_path: Path) -> None:
    custom_venv = "/opt/research/mutation-env"
    result, fake, _, _, _ = run_backend(tmp_path, mutation_venv=custom_venv)

    flattened = [part for command in fake.commands for part in command]
    assert result.status is MutationStatus.COMPLETE
    assert result.mutation_venv == custom_venv
    assert f"{custom_venv}/bin/python" in flattened
    assert f"{custom_venv}/bin/pytest" in flattened
    assert f"{custom_venv}/bin/mutmut" in flattened


def test_backend_rejects_relative_mutation_virtualenv() -> None:
    with pytest.raises(ValueError, match="absolute WSL path"):
        WSLMutmutBackend(mutation_venv="relative/env")


def test_missing_virtualenv_python_names_the_absolute_executable(tmp_path: Path) -> None:
    result, _, _, _, _ = run_backend(tmp_path, "python-unavailable")

    assert result.status is MutationStatus.TOOL_UNAVAILABLE
    assert f"{DEFAULT_MUTATION_VENV}/bin/python" in (result.error_message or "")
