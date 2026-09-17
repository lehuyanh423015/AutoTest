"""Isolated target-function line and branch coverage measurement."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autotest.errors import CoverageError
from autotest.project_analyzer import FunctionInfo
from autotest.repository_execution import RepositoryExecutionContext


@dataclass(frozen=True, slots=True)
class CoverageResult:
    """Normalized coverage.py data restricted to one function source range."""

    target_file: Path
    function_name: str
    executable_lines: tuple[int, ...]
    executed_lines: tuple[int, ...]
    missing_lines: tuple[int, ...]
    executed_branches: tuple[tuple[int, int], ...]
    missing_branches: tuple[tuple[int, int], ...]
    line_coverage_percent: float
    branch_coverage_percent: float | None
    raw_report_file: Path | None
    duration_seconds: float


class CoverageRunner:
    """Measure cumulative pytest files with coverage.py in child processes."""

    def __init__(
        self, timeout: float = 30.0, *, execution_context: RepositoryExecutionContext | None = None
    ) -> None:
        if timeout <= 0:
            raise CoverageError("Coverage timeout must be greater than zero.")
        self.timeout = timeout
        self.execution_context = execution_context

    def run(
        self,
        function: FunctionInfo,
        test_files: Sequence[Path | str],
        output_dir: Path | str,
    ) -> CoverageResult:
        """Execute tests under branch coverage and persist raw and normalized reports."""
        tests = tuple(Path(item).resolve() for item in test_files)
        if not tests:
            raise CoverageError("At least one accepted test file is required for coverage.")
        missing_test = next((path for path in tests if not path.is_file()), None)
        if missing_test is not None:
            raise CoverageError(f"Coverage test file does not exist: {missing_test}")
        if not function.file_path.is_file():
            raise CoverageError(f"Coverage target file does not exist: {function.file_path}")

        directory = Path(output_dir).resolve()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CoverageError(f"Could not create coverage artifact directory: {exc}") from exc

        data_file = directory / ".coverage-autotest"
        raw_file = directory / "coverage_raw.json"
        normalized_file = directory / "coverage_result.json"
        stdout_file = directory / "stdout.txt"
        stderr_file = directory / "stderr.txt"
        for path in (data_file, raw_file, normalized_file, stdout_file, stderr_file):
            if path.exists():
                raise CoverageError(f"Refusing to overwrite coverage artifact: {path}")

        target_root = function.file_path.parent.resolve()
        if self.execution_context is None:
            environment = os.environ.copy()
            existing_pythonpath = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                str(target_root)
                if not existing_pythonpath
                else os.pathsep.join((str(target_root), existing_pythonpath))
            )
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            python = sys.executable
            source = str(target_root)
            config_args: list[str] = []
            cwd = directory
        else:
            environment = self.execution_context.environment()
            python = str(self.execution_context.python_executable)
            source = ",".join(str(path) for path in self.execution_context.source_roots)
            config_args = ["-c", str(self.execution_context.pytest_config)]
            cwd = self.execution_context.working_directory
        environment["COVERAGE_FILE"] = str(data_file)

        run_command = [
            python,
            "-m",
            "coverage",
            "run",
            "--branch",
            "--data-file",
            str(data_file),
            "--source",
            source,
            "-m",
            "pytest",
            *config_args,
            "--rootdir",
            str(directory),
            "--import-mode=importlib",
            *(str(path) for path in tests),
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        report_command = [
            python,
            "-m",
            "coverage",
            "json",
            "--data-file",
            str(data_file),
            "--pretty-print",
            "-o",
            str(raw_file),
        ]
        started = time.perf_counter()
        stdout = ""
        stderr = ""
        try:
            measured = self._execute(run_command, cwd, environment)
            stdout = measured.stdout
            stderr = measured.stderr
            if measured.returncode != 0:
                raise CoverageError(
                    f"Coverage pytest execution failed with exit code {measured.returncode}."
                )

            reported = self._execute(report_command, cwd, environment)
            stdout += reported.stdout
            stderr += reported.stderr
            if reported.returncode != 0:
                raise CoverageError(
                    f"Coverage JSON reporting failed with exit code {reported.returncode}."
                )
            duration = time.perf_counter() - started
            raw = self._load_report(raw_file)
            result = self.parse_report(function, raw, raw_file, duration)
            self._write_json_new(normalized_file, self.to_dict(result))
            return result
        except subprocess.TimeoutExpired as exc:
            stdout += self._output_text(exc.stdout)
            stderr += self._output_text(exc.stderr)
            raise CoverageError(
                f"Coverage measurement timed out after {self.timeout:g} seconds."
            ) from exc
        finally:
            try:
                self._write_text_if_absent(stdout_file, stdout)
                self._write_text_if_absent(stderr_file, stderr)
            finally:
                try:
                    data_file.unlink(missing_ok=True)
                except OSError:
                    pass

    def _execute(
        self,
        command: list[str],
        cwd: Path,
        environment: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )

    @classmethod
    def parse_report(
        cls,
        function: FunctionInfo,
        raw_report: Mapping[str, Any],
        raw_report_file: Path | None = None,
        duration_seconds: float = 0.0,
    ) -> CoverageResult:
        """Normalize one coverage.py 7.16 JSON report without importing target code."""
        files = raw_report.get("files")
        if not isinstance(files, Mapping):
            raise CoverageError("Coverage JSON does not contain a valid files mapping.")
        target_data = cls._target_data(files, function.file_path, raw_report_file)
        executed = cls._integer_tuple(target_data, "executed_lines")
        missing = cls._integer_tuple(target_data, "missing_lines")
        start, end = function.start_line, function.end_line
        executed_lines = tuple(line for line in executed if start <= line <= end)
        missing_lines = tuple(line for line in missing if start <= line <= end)
        executable_lines = tuple(sorted(set(executed_lines) | set(missing_lines)))

        executed_branches = cls._arc_tuple(target_data, "executed_branches", start, end)
        missing_branches = cls._arc_tuple(target_data, "missing_branches", start, end)
        all_branches = set(executed_branches) | set(missing_branches)
        line_percent = (
            100.0 * len(executed_lines) / len(executable_lines) if executable_lines else 100.0
        )
        branch_percent = (
            100.0 * len(executed_branches) / len(all_branches) if all_branches else None
        )
        return CoverageResult(
            target_file=function.file_path.resolve(),
            function_name=function.function_name,
            executable_lines=executable_lines,
            executed_lines=executed_lines,
            missing_lines=missing_lines,
            executed_branches=executed_branches,
            missing_branches=missing_branches,
            line_coverage_percent=line_percent,
            branch_coverage_percent=branch_percent,
            raw_report_file=raw_report_file.resolve() if raw_report_file is not None else None,
            duration_seconds=duration_seconds,
        )

    @staticmethod
    def to_dict(result: CoverageResult) -> dict[str, Any]:
        """Return compact JSON-safe normalized coverage metadata."""
        return {
            "target_file": str(result.target_file),
            "function_name": result.function_name,
            "executable_lines": list(result.executable_lines),
            "executed_lines": list(result.executed_lines),
            "missing_lines": list(result.missing_lines),
            "executed_branches": [list(arc) for arc in result.executed_branches],
            "missing_branches": [list(arc) for arc in result.missing_branches],
            "line_coverage_percent": result.line_coverage_percent,
            "branch_coverage_percent": result.branch_coverage_percent,
            "raw_report_file": (
                str(result.raw_report_file) if result.raw_report_file is not None else None
            ),
            "duration_seconds": result.duration_seconds,
        }

    @staticmethod
    def _target_data(
        files: Mapping[str, Any], target_file: Path, raw_report_file: Path | None
    ) -> Mapping[str, Any]:
        target_key = os.path.normcase(str(target_file.resolve()))
        base = raw_report_file.parent if raw_report_file is not None else Path.cwd()
        for reported_path, data in files.items():
            path = Path(reported_path)
            resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
            if os.path.normcase(str(resolved)) == target_key:
                if not isinstance(data, Mapping):
                    break
                return data
        raise CoverageError(f"Coverage JSON has no data for target file: {target_file}")

    @staticmethod
    def _integer_tuple(data: Mapping[str, Any], key: str) -> tuple[int, ...]:
        values = data.get(key)
        if not isinstance(values, list) or any(type(item) is not int for item in values):
            raise CoverageError(f"Coverage JSON has invalid {key} data.")
        return tuple(sorted(set(values)))

    @staticmethod
    def _arc_tuple(
        data: Mapping[str, Any], key: str, start: int, end: int
    ) -> tuple[tuple[int, int], ...]:
        values = data.get(key)
        if not isinstance(values, list):
            raise CoverageError(f"Coverage JSON has invalid {key} data.")
        arcs: set[tuple[int, int]] = set()
        for item in values:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or type(item[0]) is not int
                or type(item[1]) is not int
            ):
                raise CoverageError(f"Coverage JSON has invalid {key} arc data.")
            arc = (item[0], item[1])
            if start <= arc[0] <= end:
                arcs.add(arc)
        return tuple(sorted(arcs))

    @staticmethod
    def _load_report(path: Path) -> Mapping[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CoverageError(f"Could not read coverage JSON report: {exc}") from exc
        if not isinstance(data, Mapping):
            raise CoverageError("Coverage JSON root must be an object.")
        return data

    @staticmethod
    def _write_json_new(path: Path, data: Mapping[str, Any]) -> None:
        CoverageRunner._write_text_new(path, f"{json.dumps(data, ensure_ascii=False, indent=2)}\n")

    @staticmethod
    def _write_text_new(path: Path, content: str) -> None:
        try:
            with path.open("x", encoding="utf-8", newline="") as artifact:
                artifact.write(content)
        except FileExistsError as exc:
            raise CoverageError(f"Refusing to overwrite coverage artifact: {path}") from exc
        except (OSError, UnicodeError) as exc:
            raise CoverageError(f"Could not write coverage artifact {path}: {exc}") from exc

    @staticmethod
    def _write_text_if_absent(path: Path, content: str) -> None:
        if path.exists():
            return
        CoverageRunner._write_text_new(path, content)

    @staticmethod
    def _output_text(output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, bytes):
            return output.decode(errors="replace")
        return output
