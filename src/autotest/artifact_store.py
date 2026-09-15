"""Immutable, UTF-8 run artifacts for reproducible Phase 1 experiments."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autotest.errors import ArtifactError
from autotest.project_analyzer import FunctionInfo
from autotest.test_generator import GeneratedTest
from autotest.test_runner import TestRunResult

if TYPE_CHECKING:
    from autotest.repair_engine import RepairSessionResult, TestAttempt

_RUN_ID_PATTERN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{6}")


@dataclass(frozen=True, slots=True)
class RunArtifacts:
    """Filesystem locations reserved for one non-overwriting generation run."""

    run_id: str
    timestamp_utc: str
    run_dir: Path
    prompt_file: Path
    raw_response_file: Path
    generated_test_file: Path
    result_file: Path


@dataclass(frozen=True, slots=True)
class AttemptArtifacts:
    """Immutable locations for one initial or repair attempt."""

    attempt_index: int
    attempt_dir: Path
    prompt_file: Path
    raw_response_file: Path
    generated_test_file: Path
    stdout_file: Path
    stderr_file: Path
    result_file: Path


class ArtifactStore:
    """Create unique run directories and write their small artifact set."""

    def __init__(self, root: Path | str = Path("workspace/runs")) -> None:
        self.root = Path(root).resolve()

    def create_run(
        self,
        run_id: str | None = None,
        *,
        timestamp_utc: str | None = None,
    ) -> RunArtifacts:
        """Atomically reserve a unique run directory without overwriting prior work."""
        now = datetime.now(timezone.utc)
        timestamp = timestamp_utc or now.isoformat(timespec="seconds").replace("+00:00", "Z")

        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ArtifactError(f"Could not create artifact root {self.root}: {exc}") from exc

        if run_id is not None:
            self._validate_run_id(run_id)
            try:
                return self._reserve(run_id, timestamp)
            except FileExistsError as exc:
                raise ArtifactError(
                    f"Refusing to overwrite existing run directory: {self.root / run_id}"
                ) from exc

        timestamp_part = now.strftime("%Y%m%dT%H%M%SZ")
        for _ in range(10):
            candidate = f"{timestamp_part}-{secrets.token_hex(3)}"
            try:
                return self._reserve(candidate, timestamp)
            except FileExistsError:
                continue
        raise ArtifactError("Could not allocate a unique run directory after 10 attempts.")

    def save_generation(self, run: RunArtifacts, generated: GeneratedTest) -> GeneratedTest:
        """Save prompt, raw response, and generated test without replacing existing artifacts."""
        self._write_new_text(run.prompt_file, generated.prompt)
        self._write_new_text(run.raw_response_file, generated.raw_response)

        generated_path = generated.test_file.resolve()
        if generated_path == run.generated_test_file:
            try:
                saved_code = run.generated_test_file.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise ArtifactError(
                    f"Could not verify generated test {run.generated_test_file}: {exc}"
                ) from exc
            if saved_code != generated.test_code:
                raise ArtifactError("Generated test artifact does not match the generated source.")
        else:
            self._write_new_text(run.generated_test_file, generated.test_code)

        return GeneratedTest(
            target_function=generated.target_function,
            test_file=run.generated_test_file,
            prompt=generated.prompt,
            raw_response=generated.raw_response,
            test_code=generated.test_code,
            generation_duration_seconds=generated.generation_duration_seconds,
        )

    def create_attempt(self, run: RunArtifacts, attempt_index: int) -> AttemptArtifacts:
        """Reserve a deterministic attempt directory inside an existing run."""
        if attempt_index < 0:
            raise ArtifactError("Attempt index must be greater than or equal to zero.")
        attempt_dir = run.run_dir / f"attempt-{attempt_index:03d}"
        try:
            attempt_dir.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise ArtifactError(
                f"Refusing to overwrite existing attempt directory: {attempt_dir}"
            ) from exc
        except OSError as exc:
            raise ArtifactError(f"Could not create attempt directory {attempt_dir}: {exc}") from exc
        return AttemptArtifacts(
            attempt_index=attempt_index,
            attempt_dir=attempt_dir,
            prompt_file=attempt_dir / "prompt.txt",
            raw_response_file=attempt_dir / "raw_response.txt",
            generated_test_file=attempt_dir / "generated_test.py",
            stdout_file=attempt_dir / "stdout.txt",
            stderr_file=attempt_dir / "stderr.txt",
            result_file=attempt_dir / "result.json",
        )

    def save_attempt_prompt(self, attempt: AttemptArtifacts, prompt: str) -> None:
        """Persist the exact prompt before invoking the provider."""
        self._write_new_text(attempt.prompt_file, prompt)

    def save_attempt_raw_response(self, attempt: AttemptArtifacts, raw_response: str) -> None:
        """Persist the exact provider output before sanitization or analysis."""
        self._write_new_text(attempt.raw_response_file, raw_response)

    def save_attempt_test(self, attempt: AttemptArtifacts, test_code: str) -> Path:
        """Persist sanitized generated test source without overwriting."""
        self._write_new_text(attempt.generated_test_file, test_code)
        return attempt.generated_test_file

    def save_attempt_result(
        self,
        attempt_artifacts: AttemptArtifacts,
        attempt: TestAttempt,
        *,
        execution_timeout_seconds: float,
    ) -> dict[str, Any]:
        """Complete an attempt with raw output and compact result metadata."""
        self._write_new_text(attempt_artifacts.stdout_file, attempt.run_result.stdout)
        self._write_new_text(attempt_artifacts.stderr_file, attempt.run_result.stderr)
        failure_category = (
            None
            if attempt.run_result.status.value == "PASS"
            else attempt.failure_context.category.value
        )
        metadata: dict[str, Any] = {
            "attempt_index": attempt.attempt_index,
            "attempt_kind": attempt.kind.value,
            "execution": {
                "status": attempt.run_result.status.value,
                "exit_code": attempt.run_result.exit_code,
                "duration_seconds": attempt.run_result.duration_seconds,
                "timeout_seconds": execution_timeout_seconds,
                "failure_category": failure_category,
            },
            "generation": {
                "duration_seconds": attempt.generation_duration_seconds,
                "test_function_count": attempt.structure.test_function_count,
                "assert_count": attempt.structure.assert_count,
                "quality_warnings": list(attempt.quality_warnings),
            },
            "files": {
                "prompt": attempt_artifacts.prompt_file.name,
                "raw_response": attempt_artifacts.raw_response_file.name,
                "generated_test": attempt_artifacts.generated_test_file.name,
                "stdout": attempt_artifacts.stdout_file.name,
                "stderr": attempt_artifacts.stderr_file.name,
            },
            "hashes": {
                "prompt_sha256": self._sha256(attempt_artifacts.prompt_file),
                "generated_test_sha256": self._sha256(attempt_artifacts.generated_test_file),
            },
        }
        self._write_new_text(
            attempt_artifacts.result_file,
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n",
        )
        return metadata

    def save_session_result(
        self,
        run: RunArtifacts,
        function: FunctionInfo,
        session: RepairSessionResult,
        *,
        provider: str,
        model: str,
        base_url: str,
        http_timeout_seconds: float,
        temperature: float,
        max_repair_attempts: int,
        execution_timeout_seconds: float,
    ) -> dict[str, Any]:
        """Persist the root summary for a complete bounded repair session."""
        initial_attempt = session.attempts[0]
        final_attempt = session.attempts[-1]
        source_hash = self._sha256(function.file_path)
        metadata: dict[str, Any] = {
            "run_id": run.run_id,
            "timestamp_utc": run.timestamp_utc,
            "target": {
                "file": str(function.file_path),
                "function": function.function_name,
                "start_line": function.start_line,
                "end_line": function.end_line,
            },
            "llm": {
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "http_timeout_seconds": http_timeout_seconds,
                "temperature": temperature,
            },
            "repair": {
                "enabled": max_repair_attempts > 0,
                "max_repair_attempts": max_repair_attempts,
                "repair_count": session.repair_count,
                "repair_success": session.repaired_successfully,
                "repaired_successfully": session.repaired_successfully,
                "stop_reason": session.stopped_reason.value,
            },
            "generation": {
                "prompt_file": f"attempt-{initial_attempt.attempt_index:03d}/prompt.txt",
                "raw_response_file": (
                    f"attempt-{initial_attempt.attempt_index:03d}/raw_response.txt"
                ),
                "generated_test_file": (
                    f"attempt-{initial_attempt.attempt_index:03d}/generated_test.py"
                ),
            },
            "execution": {
                "status": session.final_status.value,
                "exit_code": final_attempt.run_result.exit_code,
                "duration_seconds": final_attempt.run_result.duration_seconds,
                "timeout_seconds": execution_timeout_seconds,
                "initial_status": session.initial_status.value,
                "final_status": session.final_status.value,
                "total_generation_seconds": session.total_generation_seconds,
                "total_execution_seconds": session.total_execution_seconds,
            },
            "hashes": {"source_sha256": source_hash},
            "attempt_count": len(session.attempts),
            "attempts": [
                {
                    "index": item.attempt_index,
                    "kind": item.kind.value,
                    "status": item.run_result.status.value,
                    "directory": f"attempt-{item.attempt_index:03d}",
                }
                for item in session.attempts
            ],
        }
        self._write_new_text(
            run.result_file,
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n",
        )
        return metadata

    def save_result(
        self,
        run: RunArtifacts,
        function: FunctionInfo,
        generated: GeneratedTest,
        execution: TestRunResult,
        *,
        provider: str,
        model: str,
        base_url: str,
        http_timeout_seconds: float,
        temperature: float,
        execution_timeout_seconds: float,
    ) -> dict[str, Any]:
        """Write result metadata and content hashes as UTF-8 JSON."""
        if generated.test_file.resolve() != run.generated_test_file:
            raise ArtifactError("Execution result does not belong to this run directory.")
        metadata: dict[str, Any] = {
            "run_id": run.run_id,
            "timestamp_utc": run.timestamp_utc,
            "target": {
                "file": str(function.file_path),
                "function": function.function_name,
                "start_line": function.start_line,
                "end_line": function.end_line,
            },
            "llm": {
                "provider": provider,
                "model": model,
                "base_url": base_url,
                "http_timeout_seconds": http_timeout_seconds,
                "temperature": temperature,
            },
            "generation": {
                "prompt_file": run.prompt_file.name,
                "raw_response_file": run.raw_response_file.name,
                "generated_test_file": run.generated_test_file.name,
            },
            "execution": {
                "status": execution.status.value,
                "exit_code": execution.exit_code,
                "duration_seconds": execution.duration_seconds,
                "timeout_seconds": execution_timeout_seconds,
            },
            "hashes": {
                "source_sha256": self._sha256(function.file_path),
                "prompt_sha256": self._sha256(run.prompt_file),
                "generated_test_sha256": self._sha256(run.generated_test_file),
            },
        }
        payload = f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n"
        self._write_new_text(run.result_file, payload)
        return metadata

    def _reserve(self, run_id: str, timestamp_utc: str) -> RunArtifacts:
        run_dir = self.root / run_id
        try:
            run_dir.mkdir(exist_ok=False)
        except FileExistsError:
            raise
        except OSError as exc:
            raise ArtifactError(f"Could not create run directory {run_dir}: {exc}") from exc
        return RunArtifacts(
            run_id=run_id,
            timestamp_utc=timestamp_utc,
            run_dir=run_dir,
            prompt_file=run_dir / "prompt.txt",
            raw_response_file=run_dir / "raw_response.txt",
            generated_test_file=run_dir / "generated_test.py",
            result_file=run_dir / "result.json",
        )

    @staticmethod
    def _validate_run_id(run_id: str) -> None:
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise ArtifactError(
                "Run ID must use the format YYYYMMDDTHHMMSSZ followed by six hex characters."
            )

    @staticmethod
    def _write_new_text(path: Path, content: str) -> None:
        try:
            with path.open("x", encoding="utf-8", newline="") as artifact:
                artifact.write(content)
        except FileExistsError as exc:
            raise ArtifactError(f"Refusing to overwrite existing run artifact: {path}") from exc
        except (OSError, UnicodeError) as exc:
            raise ArtifactError(f"Could not write run artifact {path}: {exc}") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ArtifactError(f"Could not hash artifact {path}: {exc}") from exc
