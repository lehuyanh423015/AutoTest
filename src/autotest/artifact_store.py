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
    from autotest.coverage_engine import CoverageRoundResult, CoverageSessionResult
    from autotest.mutation_runner import MutationResult
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


@dataclass(frozen=True, slots=True)
class CoverageRoundArtifacts:
    """Immutable locations for one coverage-guided candidate round."""

    round_index: int
    round_dir: Path
    prompt_file: Path
    raw_response_file: Path
    generated_test_file: Path
    result_file: Path


@dataclass(frozen=True, slots=True)
class MutationArtifacts:
    """Locations reserved for one isolated mutation evaluation."""

    mutation_dir: Path
    result_file: Path
    raw_stats_file: Path
    stdout_file: Path
    stderr_file: Path
    config_dir: Path
    workspace: Path


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

    def create_coverage_baseline(self, run: RunArtifacts) -> Path:
        """Reserve the coverage baseline directory without touching Phase 2 attempts."""
        coverage_dir = run.run_dir / "coverage"
        baseline_dir = coverage_dir / "baseline"
        try:
            coverage_dir.mkdir(exist_ok=True)
            baseline_dir.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise ArtifactError(
                f"Refusing to overwrite coverage baseline directory: {baseline_dir}"
            ) from exc
        except OSError as exc:
            raise ArtifactError(f"Could not create coverage baseline directory: {exc}") from exc
        return baseline_dir

    def create_coverage_round(self, run: RunArtifacts, round_index: int) -> CoverageRoundArtifacts:
        """Reserve one deterministic supplementary-test proposal directory."""
        if round_index < 1:
            raise ArtifactError("Coverage round index must be greater than or equal to one.")
        coverage_dir = run.run_dir / "coverage"
        round_dir = coverage_dir / f"round-{round_index:03d}"
        try:
            coverage_dir.mkdir(exist_ok=True)
            round_dir.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise ArtifactError(f"Refusing to overwrite coverage round: {round_dir}") from exc
        except OSError as exc:
            raise ArtifactError(f"Could not create coverage round: {exc}") from exc
        return CoverageRoundArtifacts(
            round_index=round_index,
            round_dir=round_dir,
            prompt_file=round_dir / "prompt.txt",
            raw_response_file=round_dir / "raw_response.txt",
            generated_test_file=round_dir / "generated_test.py",
            result_file=round_dir / "result.json",
        )

    def create_mutation(self, run: RunArtifacts) -> MutationArtifacts:
        """Reserve a fresh mutation subtree for this unique run."""
        mutation_dir = run.run_dir / "mutation"
        config_dir = mutation_dir / "config"
        try:
            mutation_dir.mkdir(exist_ok=False)
            config_dir.mkdir()
        except FileExistsError as exc:
            raise ArtifactError(
                f"Refusing to overwrite existing mutation directory: {mutation_dir}"
            ) from exc
        except OSError as exc:
            raise ArtifactError(f"Could not create mutation directory: {exc}") from exc
        return MutationArtifacts(
            mutation_dir=mutation_dir,
            result_file=mutation_dir / "mutation_result.json",
            raw_stats_file=mutation_dir / "mutmut_raw_stats.json",
            stdout_file=mutation_dir / "stdout.txt",
            stderr_file=mutation_dir / "stderr.txt",
            config_dir=config_dir,
            workspace=mutation_dir / "workspace",
        )

    def save_mutation_result(
        self, artifacts: MutationArtifacts, result: MutationResult
    ) -> dict[str, Any]:
        """Persist normalized mutation metadata and captured tool output immutably."""
        self._write_new_text(artifacts.stdout_file, result.stdout)
        self._write_new_text(artifacts.stderr_file, result.stderr)
        metadata = self._mutation_metadata(result)
        self._write_new_text(
            artifacts.result_file,
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n",
        )
        return metadata

    def save_coverage_prompt(self, artifacts: CoverageRoundArtifacts, prompt: str) -> None:
        self._write_new_text(artifacts.prompt_file, prompt)

    def save_coverage_response(self, artifacts: CoverageRoundArtifacts, response: str) -> None:
        self._write_new_text(artifacts.raw_response_file, response)

    def save_coverage_candidate(self, artifacts: CoverageRoundArtifacts, code: str) -> Path:
        self._write_new_text(artifacts.generated_test_file, code)
        return artifacts.generated_test_file

    def create_nested_run(
        self, run: RunArtifacts, parent: Path, name: str = "candidate"
    ) -> RunArtifacts:
        """Reserve a scoped Phase 2 attempt container for a coverage candidate."""
        directory = parent / name
        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError as exc:
            raise ArtifactError(f"Refusing to overwrite nested run directory: {directory}") from exc
        except OSError as exc:
            raise ArtifactError(f"Could not create nested run directory: {exc}") from exc
        return RunArtifacts(
            run_id=run.run_id,
            timestamp_utc=run.timestamp_utc,
            run_dir=directory,
            prompt_file=directory / "prompt.txt",
            raw_response_file=directory / "raw_response.txt",
            generated_test_file=directory / "generated_test.py",
            result_file=directory / "result.json",
        )

    def save_coverage_round_result(
        self, artifacts: CoverageRoundArtifacts, round_result: CoverageRoundResult
    ) -> dict[str, Any]:
        """Persist compact round evidence while raw reports remain separate."""
        before = round_result.coverage_before
        after = round_result.coverage_after
        candidate = round_result.candidate_test
        repair_session = round_result.candidate_execution
        metadata: dict[str, Any] = {
            "round_index": round_result.round_index,
            "accepted": round_result.accepted,
            "stop_reason": (
                round_result.stop_reason.value if round_result.stop_reason is not None else None
            ),
            "coverage_before": self._coverage_summary(before),
            "coverage_after": self._coverage_summary(after) if after is not None else None,
            "coverage_delta": {
                "line_percent": round_result.line_coverage_delta,
                "branch_percent": round_result.branch_coverage_delta,
            },
            "generation": {
                "duration_seconds": round_result.generation_duration_seconds,
                "test_function_count": (
                    round_result.structure.test_function_count
                    if round_result.structure is not None
                    else 0
                ),
                "assert_count": (
                    round_result.structure.assert_count if round_result.structure is not None else 0
                ),
                "quality_warnings": list(round_result.quality_warnings),
            },
            "candidate_execution": (
                {
                    "initial_status": repair_session.initial_status.value,
                    "final_status": repair_session.final_status.value,
                    "attempt_count": len(repair_session.attempts),
                    "repair_count": repair_session.repair_count,
                    "stop_reason": repair_session.stopped_reason.value,
                    "final_test_file": str(repair_session.attempts[-1].test_file),
                }
                if repair_session is not None
                else None
            ),
            "files": {
                "prompt": artifacts.prompt_file.name,
                "raw_response": (
                    artifacts.raw_response_file.name
                    if artifacts.raw_response_file.exists()
                    else None
                ),
                "generated_test": (
                    artifacts.generated_test_file.name
                    if artifacts.generated_test_file.exists()
                    else None
                ),
                "candidate_final_test": (
                    str(candidate.test_file) if candidate is not None else None
                ),
            },
            "hashes": {
                "prompt_sha256": self._sha256(artifacts.prompt_file),
                "generated_test_sha256": (
                    self._sha256(artifacts.generated_test_file)
                    if artifacts.generated_test_file.exists()
                    else None
                ),
            },
        }
        self._write_new_text(
            artifacts.result_file,
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n",
        )
        return metadata

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
        coverage_session: CoverageSessionResult | None = None,
        max_coverage_rounds: int | None = None,
        coverage_target: float | None = None,
        mutation_enabled: bool | None = None,
        mutation_timeout_seconds: float | None = None,
        mutation_result: MutationResult | None = None,
        mutation_skipped_reason: str | None = None,
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
        if max_coverage_rounds is not None and coverage_target is not None:
            metadata["coverage"] = self._coverage_session_metadata(
                session,
                coverage_session,
                max_coverage_rounds=max_coverage_rounds,
                coverage_target=coverage_target,
            )
        if mutation_enabled is not None:
            if mutation_result is None:
                metadata["mutation"] = {
                    "enabled": mutation_enabled,
                    "status": None,
                    "timeout_seconds": mutation_timeout_seconds,
                    "skipped_reason": (
                        mutation_skipped_reason
                        or ("EXECUTION_NOT_PASSING" if mutation_enabled else "NOT_REQUESTED")
                    ),
                }
            else:
                metadata["mutation"] = {
                    "enabled": True,
                    "timeout_seconds": mutation_timeout_seconds,
                    **self._mutation_metadata(mutation_result),
                }
                final_coverage = (
                    coverage_session.final_coverage if coverage_session is not None else None
                )
                metadata["mutation"]["final_line_coverage"] = (
                    final_coverage.line_coverage_percent if final_coverage is not None else None
                )
                metadata["mutation"]["final_branch_coverage"] = (
                    final_coverage.branch_coverage_percent if final_coverage is not None else None
                )
        self._write_new_text(
            run.result_file,
            f"{json.dumps(metadata, ensure_ascii=False, indent=2)}\n",
        )
        return metadata

    @staticmethod
    def _mutation_metadata(result: MutationResult) -> dict[str, Any]:
        return {
            "backend": result.backend,
            "backend_version": result.backend_version,
            "status": result.status.value,
            "total_mutants": result.total_mutants,
            "tool_total_mutants": result.tool_total_mutants,
            "killed_mutants": result.killed_mutants,
            "survived_mutants": result.survived_mutants,
            "no_tests_mutants": result.no_tests_mutants,
            "skipped_mutants": result.skipped_mutants,
            "suspicious_mutants": result.suspicious_mutants,
            "timeout_mutants": result.timeout_mutants,
            "interrupted_mutants": result.interrupted_mutants,
            "segfault_mutants": result.segfault_mutants,
            "unreported_mutants": result.unreported_mutants,
            "unknown_categories": dict(result.unknown_categories),
            "mutation_score_percent": result.mutation_score_percent,
            "duration_seconds": result.duration_seconds,
            "python_version": result.python_version,
            "pytest_version": result.pytest_version,
            "mutation_venv": result.mutation_venv,
            "raw_stats_file": (
                result.raw_stats_file.name if result.raw_stats_file is not None else None
            ),
            "workspace": str(result.workspace) if result.workspace is not None else None,
            "hashes": dict(result.hashes),
            "error_message": result.error_message,
        }

    def _coverage_session_metadata(
        self,
        repair_session: RepairSessionResult,
        coverage_session: CoverageSessionResult | None,
        *,
        max_coverage_rounds: int,
        coverage_target: float,
    ) -> dict[str, Any]:
        if coverage_session is None:
            return {
                "enabled": True,
                "target": coverage_target,
                "max_rounds": max_coverage_rounds,
                "rounds_used": 0,
                "target_reached": False,
                "stop_reason": None,
                "skipped_reason": "EXECUTION_NOT_PASSING",
            }
        initial = coverage_session.initial_coverage
        final = coverage_session.final_coverage
        accepted_rounds = sum(item.accepted for item in coverage_session.rounds)
        phase2_repairs = repair_session.repair_count
        candidate_repairs = sum(
            item.candidate_execution.repair_count
            for item in coverage_session.rounds
            if item.candidate_execution is not None
        )
        coverage_generation_calls = len(coverage_session.rounds)
        return {
            "enabled": True,
            "target": coverage_target,
            "max_rounds": max_coverage_rounds,
            "rounds_used": len(coverage_session.rounds),
            "accepted_rounds": accepted_rounds,
            "target_reached": coverage_session.target_reached,
            "stop_reason": coverage_session.stop_reason.value,
            "initial": self._coverage_summary(initial) if initial is not None else None,
            "final": self._coverage_summary(final) if final is not None else None,
            "history": [self._coverage_summary(item) for item in coverage_session.history],
            "accepted_test_files": [str(path) for path in coverage_session.accepted_test_files],
            "metrics": {
                "initial_line_coverage": (
                    initial.line_coverage_percent if initial is not None else None
                ),
                "final_line_coverage": final.line_coverage_percent if final is not None else None,
                "line_coverage_gain": (
                    final.line_coverage_percent - initial.line_coverage_percent
                    if initial is not None and final is not None
                    else None
                ),
                "initial_branch_coverage": (
                    initial.branch_coverage_percent if initial is not None else None
                ),
                "final_branch_coverage": (
                    final.branch_coverage_percent if final is not None else None
                ),
                "branch_coverage_gain": (
                    final.branch_coverage_percent - initial.branch_coverage_percent
                    if initial is not None
                    and final is not None
                    and initial.branch_coverage_percent is not None
                    and final.branch_coverage_percent is not None
                    else None
                ),
                "coverage_round_count": len(coverage_session.rounds),
                "accepted_coverage_round_count": accepted_rounds,
                "coverage_target_reached": coverage_session.target_reached,
                "initial_generation_calls": 1,
                "repair_calls": phase2_repairs,
                "coverage_generation_calls": coverage_generation_calls,
                "coverage_candidate_repair_calls": candidate_repairs,
                "total_generation_count": (
                    1 + phase2_repairs + coverage_generation_calls + candidate_repairs
                ),
                "total_repair_count": phase2_repairs + candidate_repairs,
                "total_generation_time": (
                    repair_session.total_generation_seconds
                    + sum(item.total_generation_seconds for item in coverage_session.rounds)
                ),
                "total_execution_time": (
                    repair_session.total_execution_seconds
                    + sum(item.total_execution_seconds for item in coverage_session.rounds)
                ),
                "total_coverage_measurement_time": sum(
                    item.duration_seconds
                    for item in (
                        [coverage_session.initial_coverage]
                        + [round_result.coverage_after for round_result in coverage_session.rounds]
                    )
                    if item is not None
                ),
            },
        }

    @staticmethod
    def _coverage_summary(result: Any) -> dict[str, Any]:
        return {
            "line_percent": result.line_coverage_percent,
            "branch_percent": result.branch_coverage_percent,
            "missing_lines": list(result.missing_lines),
            "missing_branches": [list(arc) for arc in result.missing_branches],
        }

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
