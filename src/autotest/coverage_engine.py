"""Bounded, transactional coverage-guided supplementary-test generation."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from autotest.artifact_store import ArtifactStore, RunArtifacts
from autotest.coverage_runner import CoverageResult
from autotest.errors import CoverageError, GenerationError
from autotest.llm.base import LLMProvider
from autotest.project_analyzer import FunctionInfo
from autotest.prompt_builder import PromptBuilder
from autotest.repair_engine import RepairEngine, RepairSessionResult, StopReason, TestExecutor
from autotest.test_generator import GeneratedTest, sanitize_test_code
from autotest.test_quality import (
    TestStructureMetrics,
    analyze_test_structure,
    oracle_quality_warnings,
)
from autotest.test_runner import TestStatus

LOGGER = logging.getLogger(__name__)


class CoverageMeasurer(Protocol):
    def run(
        self,
        function: FunctionInfo,
        test_files: Sequence[Path | str],
        output_dir: Path | str,
    ) -> CoverageResult: ...


class CoverageStopReason(StrEnum):
    TARGET_REACHED = "TARGET_REACHED"
    MAX_ROUNDS_REACHED = "MAX_ROUNDS_REACHED"
    NO_COVERAGE_IMPROVEMENT = "NO_COVERAGE_IMPROVEMENT"
    COVERAGE_REGRESSION = "COVERAGE_REGRESSION"
    CANDIDATE_NOT_ACCEPTED = "CANDIDATE_NOT_ACCEPTED"
    COVERAGE_ERROR = "COVERAGE_ERROR"
    LLM_ERROR = "LLM_ERROR"
    PIPELINE_ERROR = "PIPELINE_ERROR"


@dataclass(frozen=True, slots=True)
class CoverageRoundResult:
    round_index: int
    prompt: str
    raw_response: str | None
    candidate_test: GeneratedTest | None
    candidate_execution: RepairSessionResult | None
    coverage_before: CoverageResult
    coverage_after: CoverageResult | None
    accepted: bool
    line_coverage_delta: float | None
    branch_coverage_delta: float | None
    stop_reason: CoverageStopReason | None
    structure: TestStructureMetrics | None
    quality_warnings: tuple[str, ...]
    generation_duration_seconds: float

    @property
    def total_generation_seconds(self) -> float:
        if self.candidate_execution is not None:
            return self.candidate_execution.total_generation_seconds
        return self.generation_duration_seconds

    @property
    def total_execution_seconds(self) -> float:
        return (
            self.candidate_execution.total_execution_seconds
            if self.candidate_execution is not None
            else 0.0
        )


@dataclass(frozen=True, slots=True)
class CoverageSessionResult:
    initial_coverage: CoverageResult | None
    final_coverage: CoverageResult | None
    rounds: tuple[CoverageRoundResult, ...]
    accepted_test_files: tuple[Path, ...]
    history: tuple[CoverageResult, ...]
    target_reached: bool
    stop_reason: CoverageStopReason


class CoverageEngine:
    """Generate only coverage-improving additions to a passing accepted suite."""

    def __init__(
        self,
        provider: LLMProvider,
        runner: TestExecutor,
        coverage_runner: CoverageMeasurer,
        artifact_store: ArtifactStore,
        *,
        max_coverage_rounds: int = 3,
        coverage_target: float = 100.0,
        max_repair_attempts: int = 3,
        prompt_builder: PromptBuilder | None = None,
        max_accepted_test_chars: int = 12_000,
    ) -> None:
        if max_coverage_rounds < 0:
            raise ValueError("Maximum coverage rounds must be greater than or equal to zero.")
        if not 0 <= coverage_target <= 100:
            raise ValueError("Coverage target must be between zero and 100.")
        if max_repair_attempts < 0:
            raise ValueError("Maximum repair attempts must be greater than or equal to zero.")
        if max_accepted_test_chars < 1:
            raise ValueError("Accepted-test context limit must be greater than zero.")
        self.provider = provider
        self.runner = runner
        self.coverage_runner = coverage_runner
        self.artifact_store = artifact_store
        self.max_coverage_rounds = max_coverage_rounds
        self.coverage_target = float(coverage_target)
        self.max_repair_attempts = max_repair_attempts
        self.prompt_builder = prompt_builder or PromptBuilder()
        self.max_accepted_test_chars = max_accepted_test_chars

    def run(
        self,
        function: FunctionInfo,
        run: RunArtifacts,
        phase2_session: RepairSessionResult,
    ) -> CoverageSessionResult:
        """Measure a baseline, then evaluate at most the configured number of proposals."""
        if phase2_session.final_status is not TestStatus.PASS:
            raise CoverageError("Coverage requires a passing Phase 2 test suite.")
        accepted = [phase2_session.attempts[-1].test_file]
        baseline_dir = self.artifact_store.create_coverage_baseline(run)
        try:
            baseline = self.coverage_runner.run(function, accepted, baseline_dir)
        except CoverageError:
            return CoverageSessionResult(
                None,
                None,
                (),
                tuple(accepted),
                (),
                False,
                CoverageStopReason.COVERAGE_ERROR,
            )
        history = [baseline]
        rounds: list[CoverageRoundResult] = []
        if self._target_reached(baseline):
            return self._session(
                baseline, history, rounds, accepted, CoverageStopReason.TARGET_REACHED
            )
        if self.max_coverage_rounds == 0:
            return self._session(
                baseline, history, rounds, accepted, CoverageStopReason.MAX_ROUNDS_REACHED
            )

        current = baseline
        for round_index in range(1, self.max_coverage_rounds + 1):
            artifacts = self.artifact_store.create_coverage_round(run, round_index)
            accepted_sources = tuple(path.read_text(encoding="utf-8") for path in accepted)
            prompt = self.prompt_builder.build_coverage(
                function,
                current,
                accepted_sources,
                round_index,
                max_accepted_test_chars=self.max_accepted_test_chars,
            )
            self.artifact_store.save_coverage_prompt(artifacts, prompt)
            started = time.perf_counter()
            try:
                raw_response = self.provider.generate(prompt)
            except Exception as exc:
                LOGGER.error("Coverage generation failed: %s", exc)
                round_result = self._failed_round(
                    round_index, prompt, current, CoverageStopReason.LLM_ERROR
                )
                self.artifact_store.save_coverage_round_result(artifacts, round_result)
                rounds.append(round_result)
                return self._session(
                    baseline, history, rounds, accepted, CoverageStopReason.LLM_ERROR
                )
            generation_duration = time.perf_counter() - started
            self.artifact_store.save_coverage_response(artifacts, raw_response)
            code = sanitize_test_code(raw_response)
            self.artifact_store.save_coverage_candidate(artifacts, code)
            structure = analyze_test_structure(code)
            warnings = oracle_quality_warnings(None, code)
            if not code or (
                structure.test_function_count == 0 and "INVALID_PYTHON" not in warnings
            ):
                round_result = CoverageRoundResult(
                    round_index,
                    prompt,
                    raw_response,
                    None,
                    None,
                    current,
                    None,
                    False,
                    None,
                    None,
                    CoverageStopReason.CANDIDATE_NOT_ACCEPTED,
                    structure,
                    warnings + (("NO_PYTEST_TEST_FUNCTIONS",) if code else ("EMPTY_CANDIDATE",)),
                    generation_duration,
                )
                self.artifact_store.save_coverage_round_result(artifacts, round_result)
                rounds.append(round_result)
                return self._session(
                    baseline,
                    history,
                    rounds,
                    accepted,
                    CoverageStopReason.CANDIDATE_NOT_ACCEPTED,
                )

            candidate = GeneratedTest(
                function.function_name,
                artifacts.generated_test_file,
                prompt,
                raw_response,
                code,
                generation_duration,
            )
            nested_run = self.artifact_store.create_nested_run(run, artifacts.round_dir)
            try:
                candidate_execution = RepairEngine(
                    self.provider,
                    self.runner,
                    self.artifact_store,
                    max_repair_attempts=self.max_repair_attempts,
                    prompt_builder=self.prompt_builder,
                ).run(
                    function,
                    nested_run,
                    initial_test=candidate,
                    accepted_test_files=accepted,
                )
            except GenerationError:
                candidate_execution = None
            if (
                candidate_execution is None
                or candidate_execution.final_status is not TestStatus.PASS
                or candidate_execution.attempts[-1].structure.test_function_count == 0
            ):
                stop = (
                    CoverageStopReason.LLM_ERROR
                    if candidate_execution is not None
                    and candidate_execution.stopped_reason is StopReason.LLM_ERROR
                    else CoverageStopReason.CANDIDATE_NOT_ACCEPTED
                )
                round_result = CoverageRoundResult(
                    round_index,
                    prompt,
                    raw_response,
                    candidate,
                    candidate_execution,
                    current,
                    None,
                    False,
                    None,
                    None,
                    stop,
                    structure,
                    warnings,
                    generation_duration,
                )
                self.artifact_store.save_coverage_round_result(artifacts, round_result)
                rounds.append(round_result)
                return self._session(baseline, history, rounds, accepted, stop)

            final_candidate = candidate_execution.attempts[-1]
            accepted_candidate = GeneratedTest(
                function.function_name,
                final_candidate.test_file,
                final_candidate.prompt,
                final_candidate.raw_response,
                final_candidate.test_code,
                final_candidate.generation_duration_seconds,
            )
            try:
                measured = self.coverage_runner.run(
                    function, [*accepted, accepted_candidate.test_file], artifacts.round_dir
                )
            except CoverageError:
                round_result = CoverageRoundResult(
                    round_index,
                    prompt,
                    raw_response,
                    accepted_candidate,
                    candidate_execution,
                    current,
                    None,
                    False,
                    None,
                    None,
                    CoverageStopReason.COVERAGE_ERROR,
                    final_candidate.structure,
                    final_candidate.quality_warnings,
                    generation_duration,
                )
                self.artifact_store.save_coverage_round_result(artifacts, round_result)
                rounds.append(round_result)
                return self._session(
                    baseline, history, rounds, accepted, CoverageStopReason.COVERAGE_ERROR
                )

            coverage_before = current
            line_delta = measured.line_coverage_percent - coverage_before.line_coverage_percent
            branch_delta = self._branch_delta(coverage_before, measured)
            if self._regressed(coverage_before, measured):
                stop = CoverageStopReason.COVERAGE_REGRESSION
                accepted_round = False
            elif not self._improved(coverage_before, measured):
                stop = CoverageStopReason.NO_COVERAGE_IMPROVEMENT
                accepted_round = False
            else:
                stop = None
                accepted_round = True
                accepted.append(accepted_candidate.test_file)
                current = measured
                history.append(measured)
            round_result = CoverageRoundResult(
                round_index,
                prompt,
                raw_response,
                accepted_candidate,
                candidate_execution,
                coverage_before,
                measured,
                accepted_round,
                line_delta,
                branch_delta,
                stop,
                final_candidate.structure,
                final_candidate.quality_warnings,
                generation_duration,
            )
            self.artifact_store.save_coverage_round_result(artifacts, round_result)
            rounds.append(round_result)
            if stop is not None:
                return self._session(baseline, history, rounds, accepted, stop)
            if self._target_reached(current):
                return self._session(
                    baseline, history, rounds, accepted, CoverageStopReason.TARGET_REACHED
                )

        return self._session(
            baseline, history, rounds, accepted, CoverageStopReason.MAX_ROUNDS_REACHED
        )

    def _session(
        self,
        baseline: CoverageResult,
        history: list[CoverageResult],
        rounds: list[CoverageRoundResult],
        accepted: list[Path],
        reason: CoverageStopReason,
    ) -> CoverageSessionResult:
        return CoverageSessionResult(
            baseline,
            history[-1],
            tuple(rounds),
            tuple(accepted),
            tuple(history),
            self._target_reached(history[-1]),
            reason,
        )

    def _target_reached(self, result: CoverageResult) -> bool:
        return result.line_coverage_percent >= self.coverage_target and (
            result.branch_coverage_percent is None
            or result.branch_coverage_percent >= self.coverage_target
        )

    @staticmethod
    def _improved(before: CoverageResult, after: CoverageResult) -> bool:
        return (
            after.line_coverage_percent > before.line_coverage_percent
            or len(after.missing_lines) < len(before.missing_lines)
            or (
                before.branch_coverage_percent is not None
                and after.branch_coverage_percent is not None
                and (
                    after.branch_coverage_percent > before.branch_coverage_percent
                    or len(after.missing_branches) < len(before.missing_branches)
                )
            )
        )

    @staticmethod
    def _regressed(before: CoverageResult, after: CoverageResult) -> bool:
        return (
            after.line_coverage_percent < before.line_coverage_percent
            or not set(before.executed_lines).issubset(after.executed_lines)
            or (
                before.branch_coverage_percent is not None
                and (
                    after.branch_coverage_percent is None
                    or after.branch_coverage_percent < before.branch_coverage_percent
                    or not set(before.executed_branches).issubset(after.executed_branches)
                )
            )
        )

    @staticmethod
    def _branch_delta(before: CoverageResult, after: CoverageResult) -> float | None:
        if before.branch_coverage_percent is None or after.branch_coverage_percent is None:
            return None
        return after.branch_coverage_percent - before.branch_coverage_percent

    @staticmethod
    def _failed_round(
        round_index: int,
        prompt: str,
        before: CoverageResult,
        reason: CoverageStopReason,
    ) -> CoverageRoundResult:
        return CoverageRoundResult(
            round_index,
            prompt,
            None,
            None,
            None,
            before,
            None,
            False,
            None,
            None,
            reason,
            None,
            (),
            0.0,
        )
