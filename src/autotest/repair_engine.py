"""Bounded, provider-neutral execution-feedback loop for generated tests."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from autotest.artifact_store import ArtifactStore, RunArtifacts
from autotest.errors import GenerationError, LLMError
from autotest.failure_analyzer import FailureAnalyzer, FailureContext
from autotest.llm.base import LLMProvider
from autotest.project_analyzer import FunctionInfo
from autotest.prompt_builder import PromptBuilder
from autotest.test_generator import GeneratedTest, sanitize_test_code
from autotest.test_quality import (
    TestStructureMetrics,
    analyze_test_structure,
    oracle_quality_warnings,
)
from autotest.test_runner import TestRunResult, TestStatus

LOGGER = logging.getLogger(__name__)


class TestExecutor(Protocol):
    """The runner behavior required by the repair engine."""

    timeout: float

    def run(self, test_file: Path | str, project_root: Path | str) -> TestRunResult:
        """Execute one generated test artifact."""
        ...

    def run_suite(
        self, test_files: Sequence[Path | str], project_root: Path | str
    ) -> TestRunResult:
        """Execute a cumulative generated-test suite."""
        ...


class AttemptKind(StrEnum):
    INITIAL = "INITIAL"
    REPAIR = "REPAIR"


class StopReason(StrEnum):
    PASS_REACHED = "PASS_REACHED"
    MAX_REPAIRS_REACHED = "MAX_REPAIRS_REACHED"
    LLM_ERROR = "LLM_ERROR"
    PIPELINE_ERROR = "PIPELINE_ERROR"


@dataclass(frozen=True, slots=True)
class TestAttempt:
    """One immutable generated test and execution outcome."""

    attempt_index: int
    kind: AttemptKind
    test_code: str
    test_file: Path
    prompt: str
    raw_response: str
    run_result: TestRunResult
    failure_context: FailureContext
    structure: TestStructureMetrics
    quality_warnings: tuple[str, ...]
    generation_duration_seconds: float


@dataclass(frozen=True, slots=True)
class RepairSessionResult:
    """Programmatic history and aggregate outcome of a bounded repair loop."""

    attempts: tuple[TestAttempt, ...]
    initial_status: TestStatus
    final_status: TestStatus
    repair_count: int
    repaired_successfully: bool
    stopped_reason: StopReason
    total_generation_seconds: float
    total_execution_seconds: float


class RepairEngine:
    """Generate, execute, and conservatively repair tests within a fixed bound."""

    def __init__(
        self,
        provider: LLMProvider,
        runner: TestExecutor,
        artifact_store: ArtifactStore,
        *,
        max_repair_attempts: int = 3,
        max_feedback_chars: int = 12_000,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError("Maximum repair attempts must be greater than or equal to zero.")
        self.provider = provider
        self.runner = runner
        self.artifact_store = artifact_store
        self.max_repair_attempts = max_repair_attempts
        self.failure_analyzer = FailureAnalyzer(max_feedback_chars)
        self.prompt_builder = prompt_builder or PromptBuilder()

    def run(
        self,
        function: FunctionInfo,
        run: RunArtifacts,
        initial_test: GeneratedTest | None = None,
        accepted_test_files: Sequence[Path | str] = (),
    ) -> RepairSessionResult:
        """Execute initial generation and at most the configured number of repairs."""
        attempts: list[TestAttempt] = []
        initial_prompt = (
            initial_test.prompt if initial_test is not None else self.prompt_builder.build(function)
        )
        initial = self._create_attempt(
            function=function,
            run=run,
            attempt_index=0,
            kind=AttemptKind.INITIAL,
            prompt=initial_prompt,
            previous_code=None,
            supplied_generation=initial_test,
            accepted_test_files=accepted_test_files,
        )
        attempts.append(initial)
        LOGGER.info("Initial execution completed with status %s", initial.run_result.status.value)

        if initial.run_result.status is TestStatus.PASS:
            return self._session(attempts, StopReason.PASS_REACHED)

        for repair_index in range(1, self.max_repair_attempts + 1):
            previous = attempts[-1]
            repair_prompt = self.prompt_builder.build_repair(
                function,
                previous.test_code,
                previous.failure_context,
                attempt_number=repair_index,
            )
            LOGGER.info("Repair attempt %d/%d started", repair_index, self.max_repair_attempts)
            try:
                repaired = self._create_attempt(
                    function=function,
                    run=run,
                    attempt_index=repair_index,
                    kind=AttemptKind.REPAIR,
                    prompt=repair_prompt,
                    previous_code=previous.test_code,
                    accepted_test_files=accepted_test_files,
                )
            except (LLMError, GenerationError) as exc:
                LOGGER.error("Repair generation stopped: %s", exc)
                return self._session(attempts, StopReason.LLM_ERROR)
            attempts.append(repaired)
            LOGGER.info(
                "Repair attempt %d completed with status %s",
                repair_index,
                repaired.run_result.status.value,
            )
            if repaired.run_result.status is TestStatus.PASS:
                return self._session(attempts, StopReason.PASS_REACHED)

        return self._session(attempts, StopReason.MAX_REPAIRS_REACHED)

    def _create_attempt(
        self,
        *,
        function: FunctionInfo,
        run: RunArtifacts,
        attempt_index: int,
        kind: AttemptKind,
        prompt: str,
        previous_code: str | None,
        supplied_generation: GeneratedTest | None = None,
        accepted_test_files: Sequence[Path | str] = (),
    ) -> TestAttempt:
        artifacts = self.artifact_store.create_attempt(run, attempt_index)
        self.artifact_store.save_attempt_prompt(artifacts, prompt)

        if supplied_generation is None:
            started = time.perf_counter()
            try:
                raw_response = self.provider.generate(prompt)
            except LLMError:
                raise
            except Exception as exc:
                raise GenerationError(f"LLM provider failed unexpectedly: {exc}") from exc
            generation_duration = time.perf_counter() - started
            self.artifact_store.save_attempt_raw_response(artifacts, raw_response)
            test_code = sanitize_test_code(raw_response)
        else:
            raw_response = supplied_generation.raw_response
            generation_duration = supplied_generation.generation_duration_seconds
            self.artifact_store.save_attempt_raw_response(artifacts, raw_response)
            test_code = supplied_generation.test_code

        if not test_code:
            raise GenerationError("The LLM provider returned no test code.")
        test_file = self.artifact_store.save_attempt_test(artifacts, test_code)
        if accepted_test_files:
            run_result = self.runner.run_suite(
                (*accepted_test_files, test_file), project_root=function.file_path.parent
            )
        else:
            run_result = self.runner.run(test_file, project_root=function.file_path.parent)
        failure_context = self.failure_analyzer.analyze(run_result)
        structure = analyze_test_structure(test_code)
        warnings = oracle_quality_warnings(previous_code, test_code)
        attempt = TestAttempt(
            attempt_index=attempt_index,
            kind=kind,
            test_code=test_code,
            test_file=test_file,
            prompt=prompt,
            raw_response=raw_response,
            run_result=run_result,
            failure_context=failure_context,
            structure=structure,
            quality_warnings=warnings,
            generation_duration_seconds=generation_duration,
        )
        self.artifact_store.save_attempt_result(
            artifacts,
            attempt,
            execution_timeout_seconds=self.runner.timeout,
        )
        return attempt

    @staticmethod
    def _session(attempts: list[TestAttempt], stopped_reason: StopReason) -> RepairSessionResult:
        initial_status = attempts[0].run_result.status
        final_status = attempts[-1].run_result.status
        return RepairSessionResult(
            attempts=tuple(attempts),
            initial_status=initial_status,
            final_status=final_status,
            repair_count=len(attempts) - 1,
            repaired_successfully=(
                initial_status is not TestStatus.PASS and final_status is TestStatus.PASS
            ),
            stopped_reason=stopped_reason,
            total_generation_seconds=sum(
                attempt.generation_duration_seconds for attempt in attempts
            ),
            total_execution_seconds=sum(
                attempt.run_result.duration_seconds for attempt in attempts
            ),
        )
