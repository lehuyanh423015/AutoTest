"""Isolated subprocess execution of generated pytest files."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from autotest.errors import TestExecutionError


class TestStatus(StrEnum):
    """High-level outcome of a pytest subprocess."""

    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class TestRunResult:
    """Captured outcome from one pytest subprocess."""

    test_file: Path
    exit_code: int | None
    status: TestStatus
    stdout: str
    stderr: str
    duration_seconds: float

    @property
    def passed(self) -> bool:
        return self.status is TestStatus.PASS


class TestRunner:
    """Run generated tests outside the AutoTest interpreter process."""

    def __init__(self, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise TestExecutionError("Test timeout must be greater than zero.")
        self.timeout = timeout

    def run(self, test_file: Path | str, project_root: Path | str) -> TestRunResult:
        test_path = Path(test_file).resolve()
        root = Path(project_root).resolve()
        if not test_path.is_file():
            raise TestExecutionError(f"Generated test file does not exist: {test_path}")
        if not root.is_dir():
            raise TestExecutionError(f"Target project directory does not exist: {root}")

        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(root)
            if not existing_pythonpath
            else os.pathsep.join((str(root), existing_pythonpath))
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "--rootdir",
            str(test_path.parent),
            str(test_path),
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        started = time.perf_counter()

        try:
            completed = subprocess.run(
                command,
                cwd=test_path.parent,
                env=environment,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - started
            return TestRunResult(
                test_file=test_path,
                exit_code=None,
                status=TestStatus.TIMEOUT,
                stdout=self._output_text(exc.stdout),
                stderr=self._output_text(exc.stderr),
                duration_seconds=duration,
            )
        except OSError as exc:
            duration = time.perf_counter() - started
            return TestRunResult(
                test_file=test_path,
                exit_code=None,
                status=TestStatus.ERROR,
                stdout="",
                stderr=f"Could not start pytest: {exc}",
                duration_seconds=duration,
            )

        status = self._classify(completed.returncode)
        return TestRunResult(
            test_file=test_path,
            exit_code=completed.returncode,
            status=status,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.perf_counter() - started,
        )

    @staticmethod
    def _classify(exit_code: int) -> TestStatus:
        if exit_code == 0:
            return TestStatus.PASS
        if exit_code == 1:
            return TestStatus.FAIL
        return TestStatus.ERROR

    @staticmethod
    def _output_text(output: str | bytes | None) -> str:
        if output is None:
            return ""
        if isinstance(output, bytes):
            return output.decode(errors="replace")
        return output
