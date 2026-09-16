"""Bounded, transactional mutation-guided supplementary-test generation."""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from autotest.artifact_store import ArtifactStore, RunArtifacts
from autotest.coverage_engine import CoverageEngine
from autotest.coverage_runner import CoverageResult
from autotest.errors import CoverageError, GenerationError, MutationError
from autotest.llm.base import LLMProvider
from autotest.mutation_runner import (
    MutationResult,
    MutationStatus,
    SurvivorEvidence,
)
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


class MutationFeedbackBackend(Protocol):
    """Phase 4A behavior plus supported survivor inspection."""

    def run(
        self,
        function: FunctionInfo,
        test_files: Sequence[Path | str],
        mutation_dir: Path | str,
    ) -> MutationResult: ...

    def extract_survivors(
        self,
        result: MutationResult,
        function: FunctionInfo,
        max_mutants: int,
    ) -> SurvivorEvidence: ...


class CoverageMeasurer(Protocol):
    def run(
        self,
        function: FunctionInfo,
        test_files: Sequence[Path | str],
        output_dir: Path | str,
    ) -> CoverageResult: ...


class MutationFeedbackStopReason(StrEnum):
    ALL_MUTANTS_KILLED = "ALL_MUTANTS_KILLED"
    MAX_ROUNDS_REACHED = "MAX_ROUNDS_REACHED"
    NO_MUTATION_IMPROVEMENT = "NO_MUTATION_IMPROVEMENT"
    MUTATION_REGRESSION = "MUTATION_REGRESSION"
    COVERAGE_REGRESSION = "COVERAGE_REGRESSION"
    CANDIDATE_NOT_ACCEPTED = "CANDIDATE_NOT_ACCEPTED"
    MUTANT_SET_CHANGED = "MUTANT_SET_CHANGED"
    MUTATION_ERROR = "MUTATION_ERROR"
    LLM_ERROR = "LLM_ERROR"
    PIPELINE_ERROR = "PIPELINE_ERROR"


@dataclass(frozen=True, slots=True)
class MutationFeedbackRoundResult:
    round_index: int
    evidence_before: SurvivorEvidence
    prompt: str
    raw_response: str | None
    candidate_test: GeneratedTest | None
    candidate_execution: RepairSessionResult | None
    coverage_before: CoverageResult
    coverage_after: CoverageResult | None
    mutation_before: MutationResult
    mutation_after: MutationResult | None
    evidence_after: SurvivorEvidence | None
    accepted: bool
    newly_killed_ids: tuple[str, ...]
    regressed_ids: tuple[str, ...]
    stop_reason: MutationFeedbackStopReason | None
    structure: TestStructureMetrics | None
    quality_warnings: tuple[str, ...]
    generation_duration_seconds: float


@dataclass(frozen=True, slots=True)
class MutationFeedbackSessionResult:
    baseline_mutation: MutationResult
    final_mutation: MutationResult
    baseline_evidence: SurvivorEvidence | None
    final_evidence: SurvivorEvidence | None
    rounds: tuple[MutationFeedbackRoundResult, ...]
    accepted_test_files: tuple[Path, ...]
    initial_coverage: CoverageResult
    final_coverage: CoverageResult
    stop_reason: MutationFeedbackStopReason

    @property
    def mutation_score_gain(self) -> float | None:
        before = self.baseline_mutation.mutation_score_percent
        after = self.final_mutation.mutation_score_percent
        return None if before is None or after is None else after - before

    @property
    def survivors_reduced(self) -> int:
        return self.baseline_mutation.survived_mutants - self.final_mutation.survived_mutants

    @property
    def accepted_round_count(self) -> int:
        return sum(item.accepted for item in self.rounds)

    @property
    def feedback_llm_calls(self) -> int:
        return len(self.rounds)

    @property
    def candidate_repair_count(self) -> int:
        return sum(
            item.candidate_execution.repair_count
            for item in self.rounds
            if item.candidate_execution is not None
        )


class MutationFeedbackEngine:
    """Accept only passing, coverage-safe candidates that kill prior survivors."""

    def __init__(
        self,
        provider: LLMProvider,
        runner: TestExecutor,
        coverage_runner: CoverageMeasurer,
        mutation_backend: MutationFeedbackBackend,
        artifact_store: ArtifactStore,
        *,
        max_mutation_rounds: int = 3,
        max_mutants_per_round: int = 5,
        max_repair_attempts: int = 3,
        max_mutation_feedback_chars: int = 12_000,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        if max_mutation_rounds < 0:
            raise ValueError("Maximum mutation rounds must be greater than or equal to zero.")
        if max_mutants_per_round < 1:
            raise ValueError("Maximum mutants per round must be greater than or equal to one.")
        if max_repair_attempts < 0:
            raise ValueError("Maximum repair attempts must be greater than or equal to zero.")
        if max_mutation_feedback_chars < 4_000:
            raise ValueError("Mutation feedback limit must be at least 4000 characters.")
        self.provider = provider
        self.runner = runner
        self.coverage_runner = coverage_runner
        self.mutation_backend = mutation_backend
        self.artifact_store = artifact_store
        self.max_mutation_rounds = max_mutation_rounds
        self.max_mutants_per_round = max_mutants_per_round
        self.max_repair_attempts = max_repair_attempts
        self.max_mutation_feedback_chars = max_mutation_feedback_chars
        self.prompt_builder = prompt_builder or PromptBuilder()

    def run(
        self,
        function: FunctionInfo,
        run: RunArtifacts,
        accepted_test_files: Sequence[Path | str],
        baseline_mutation: MutationResult,
        current_coverage: CoverageResult,
    ) -> MutationFeedbackSessionResult:
        """Use Round 0 as baseline and attempt at most the configured generations."""
        accepted = [Path(path).resolve() for path in accepted_test_files]
        protected = self._snapshot(function.file_path, accepted)
        rounds: list[MutationFeedbackRoundResult] = []
        if baseline_mutation.status is not MutationStatus.COMPLETE:
            return self._session(
                baseline_mutation,
                baseline_mutation,
                None,
                None,
                rounds,
                accepted,
                current_coverage,
                current_coverage,
                MutationFeedbackStopReason.MUTATION_ERROR,
            )
        try:
            evidence = self.mutation_backend.extract_survivors(
                baseline_mutation, function, self.max_mutants_per_round
            )
            baseline_survivors = self.artifact_store.create_mutation_feedback_baseline(run)
            self.artifact_store.save_survivor_evidence(baseline_survivors, evidence)
            self._verify(protected)
        except (MutationError, OSError):
            return self._session(
                baseline_mutation,
                baseline_mutation,
                None,
                None,
                rounds,
                accepted,
                current_coverage,
                current_coverage,
                MutationFeedbackStopReason.MUTATION_ERROR,
            )
        if not evidence.survivor_ids:
            return self._session(
                baseline_mutation,
                baseline_mutation,
                evidence,
                evidence,
                rounds,
                accepted,
                current_coverage,
                current_coverage,
                MutationFeedbackStopReason.ALL_MUTANTS_KILLED,
            )
        if self.max_mutation_rounds == 0:
            return self._session(
                baseline_mutation,
                baseline_mutation,
                evidence,
                evidence,
                rounds,
                accepted,
                current_coverage,
                current_coverage,
                MutationFeedbackStopReason.MAX_ROUNDS_REACHED,
            )

        current_mutation = baseline_mutation
        current_evidence = evidence
        coverage = current_coverage
        for round_index in range(1, self.max_mutation_rounds + 1):
            artifacts = self.artifact_store.create_mutation_feedback_round(run, round_index)
            self.artifact_store.save_survivor_evidence(artifacts.survivors_dir, current_evidence)
            accepted_sources = tuple(path.read_text(encoding="utf-8") for path in accepted)
            prompt = self.prompt_builder.build_mutation_feedback(
                function,
                accepted_sources,
                current_evidence.selected_survivors,
                round_index,
                max_chars=self.max_mutation_feedback_chars,
            )
            self.artifact_store.save_mutation_feedback_prompt(artifacts, prompt)
            started = time.perf_counter()
            try:
                raw_response = self.provider.generate(prompt)
            except Exception as exc:
                LOGGER.error("Mutation feedback generation failed: %s", exc)
                item = self._failed_round(
                    round_index,
                    current_evidence,
                    prompt,
                    current_mutation,
                    coverage,
                    MutationFeedbackStopReason.LLM_ERROR,
                )
                self.artifact_store.save_mutation_feedback_round_result(artifacts, item)
                rounds.append(item)
                return self._session_from_current(
                    baseline_mutation,
                    evidence,
                    rounds,
                    accepted,
                    current_mutation,
                    current_evidence,
                    current_coverage,
                    coverage,
                    MutationFeedbackStopReason.LLM_ERROR,
                )
            generation_duration = time.perf_counter() - started
            self.artifact_store.save_mutation_feedback_response(artifacts, raw_response)
            try:
                self._verify(protected)
            except (MutationError, OSError):
                item = self._failed_round(
                    round_index,
                    current_evidence,
                    prompt,
                    current_mutation,
                    coverage,
                    MutationFeedbackStopReason.PIPELINE_ERROR,
                )
                self.artifact_store.save_mutation_feedback_round_result(artifacts, item)
                rounds.append(item)
                return self._session_from_current(
                    baseline_mutation,
                    evidence,
                    rounds,
                    accepted,
                    current_mutation,
                    current_evidence,
                    current_coverage,
                    coverage,
                    MutationFeedbackStopReason.PIPELINE_ERROR,
                )
            code = sanitize_test_code(raw_response)
            self.artifact_store.save_mutation_feedback_candidate(artifacts, code)
            structure = analyze_test_structure(code)
            warnings = oracle_quality_warnings(None, code)
            if not code or structure.test_function_count == 0:
                warnings += ("EMPTY_CANDIDATE",) if not code else ("NO_PYTEST_TEST_FUNCTIONS",)
                item = MutationFeedbackRoundResult(
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    None,
                    None,
                    coverage,
                    None,
                    current_mutation,
                    None,
                    None,
                    False,
                    (),
                    (),
                    MutationFeedbackStopReason.CANDIDATE_NOT_ACCEPTED,
                    structure,
                    warnings,
                    generation_duration,
                )
                self.artifact_store.save_mutation_feedback_round_result(artifacts, item)
                rounds.append(item)
                return self._session_from_current(
                    baseline_mutation,
                    evidence,
                    rounds,
                    accepted,
                    current_mutation,
                    current_evidence,
                    current_coverage,
                    coverage,
                    MutationFeedbackStopReason.CANDIDATE_NOT_ACCEPTED,
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
                execution = RepairEngine(
                    self.provider,
                    self.runner,
                    self.artifact_store,
                    max_repair_attempts=self.max_repair_attempts,
                    prompt_builder=self.prompt_builder,
                ).run(function, nested_run, initial_test=candidate, accepted_test_files=accepted)
            except (GenerationError, OSError):
                execution = None
            try:
                self._verify(protected)
            except (MutationError, OSError):
                return self._record_terminal(
                    artifacts,
                    rounds,
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    candidate,
                    execution,
                    coverage,
                    None,
                    current_mutation,
                    None,
                    None,
                    MutationFeedbackStopReason.PIPELINE_ERROR,
                    structure,
                    warnings,
                    generation_duration,
                    baseline_mutation,
                    evidence,
                    accepted,
                    current_coverage,
                )
            if (
                execution is None
                or execution.final_status is not TestStatus.PASS
                or execution.attempts[-1].structure.test_function_count == 0
            ):
                reason = (
                    MutationFeedbackStopReason.LLM_ERROR
                    if execution is not None and execution.stopped_reason is StopReason.LLM_ERROR
                    else MutationFeedbackStopReason.CANDIDATE_NOT_ACCEPTED
                )
                item = MutationFeedbackRoundResult(
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    candidate,
                    execution,
                    coverage,
                    None,
                    current_mutation,
                    None,
                    None,
                    False,
                    (),
                    (),
                    reason,
                    structure,
                    warnings,
                    generation_duration,
                )
                self.artifact_store.save_mutation_feedback_round_result(artifacts, item)
                rounds.append(item)
                return self._session_from_current(
                    baseline_mutation,
                    evidence,
                    rounds,
                    accepted,
                    current_mutation,
                    current_evidence,
                    current_coverage,
                    coverage,
                    reason,
                )

            final_attempt = execution.attempts[-1]
            final_candidate = GeneratedTest(
                function.function_name,
                final_attempt.test_file,
                final_attempt.prompt,
                final_attempt.raw_response,
                final_attempt.test_code,
                final_attempt.generation_duration_seconds,
            )
            proposed = [*accepted, final_candidate.test_file]
            try:
                measured = self.coverage_runner.run(
                    function, proposed, artifacts.round_dir / "coverage"
                )
                self._verify(protected)
            except (CoverageError, MutationError, OSError):
                return self._record_terminal(
                    artifacts,
                    rounds,
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    final_candidate,
                    execution,
                    coverage,
                    None,
                    current_mutation,
                    None,
                    None,
                    MutationFeedbackStopReason.PIPELINE_ERROR,
                    final_attempt.structure,
                    final_attempt.quality_warnings,
                    generation_duration,
                    baseline_mutation,
                    evidence,
                    accepted,
                    current_coverage,
                )
            if CoverageEngine._regressed(coverage, measured):
                return self._record_terminal(
                    artifacts,
                    rounds,
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    final_candidate,
                    execution,
                    coverage,
                    measured,
                    current_mutation,
                    None,
                    None,
                    MutationFeedbackStopReason.COVERAGE_REGRESSION,
                    final_attempt.structure,
                    final_attempt.quality_warnings,
                    generation_duration,
                    baseline_mutation,
                    evidence,
                    accepted,
                    current_coverage,
                )

            mutation_artifacts = self.artifact_store.create_mutation_at(
                artifacts.round_dir / "mutation"
            )
            next_mutation = self.mutation_backend.run(
                function, proposed, mutation_artifacts.mutation_dir
            )
            self.artifact_store.save_mutation_result(mutation_artifacts, next_mutation)
            try:
                self._verify(protected)
            except (MutationError, OSError):
                return self._record_terminal(
                    artifacts,
                    rounds,
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    final_candidate,
                    execution,
                    coverage,
                    measured,
                    current_mutation,
                    next_mutation,
                    None,
                    MutationFeedbackStopReason.PIPELINE_ERROR,
                    final_attempt.structure,
                    final_attempt.quality_warnings,
                    generation_duration,
                    baseline_mutation,
                    evidence,
                    accepted,
                    current_coverage,
                )
            if next_mutation.status is not MutationStatus.COMPLETE:
                return self._record_terminal(
                    artifacts,
                    rounds,
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    final_candidate,
                    execution,
                    coverage,
                    measured,
                    current_mutation,
                    next_mutation,
                    None,
                    MutationFeedbackStopReason.MUTATION_ERROR,
                    final_attempt.structure,
                    final_attempt.quality_warnings,
                    generation_duration,
                    baseline_mutation,
                    evidence,
                    accepted,
                    current_coverage,
                )
            try:
                next_evidence = self.mutation_backend.extract_survivors(
                    next_mutation, function, self.max_mutants_per_round
                )
                mutation_survivors = mutation_artifacts.mutation_dir / "survivors"
                mutation_survivors.mkdir(exist_ok=False)
                self.artifact_store.save_survivor_evidence(mutation_survivors, next_evidence)
                self._verify(protected)
            except (MutationError, OSError):
                return self._record_terminal(
                    artifacts,
                    rounds,
                    round_index,
                    current_evidence,
                    prompt,
                    raw_response,
                    final_candidate,
                    execution,
                    coverage,
                    measured,
                    current_mutation,
                    next_mutation,
                    None,
                    MutationFeedbackStopReason.MUTATION_ERROR,
                    final_attempt.structure,
                    final_attempt.quality_warnings,
                    generation_duration,
                    baseline_mutation,
                    evidence,
                    accepted,
                    current_coverage,
                )
            if not self._universe_stable(
                current_mutation, current_evidence, next_mutation, next_evidence
            ):
                reason = MutationFeedbackStopReason.MUTANT_SET_CHANGED
                accepted_round = False
                newly_killed: tuple[str, ...] = ()
                regressed: tuple[str, ...] = ()
            else:
                prior_survivors = set(current_evidence.survivor_ids)
                after_survivors = set(next_evidence.survivor_ids)
                newly_killed = tuple(sorted(prior_survivors & set(next_evidence.killed_ids)))
                regressed = tuple(sorted(set(current_evidence.killed_ids) & after_survivors))
                score_before = current_mutation.mutation_score_percent
                score_after = next_mutation.mutation_score_percent
                if (
                    regressed
                    or next_mutation.survived_mutants > current_mutation.survived_mutants
                    or (
                        score_before is not None
                        and score_after is not None
                        and score_after < score_before
                    )
                ):
                    reason = MutationFeedbackStopReason.MUTATION_REGRESSION
                    accepted_round = False
                elif not newly_killed:
                    reason = MutationFeedbackStopReason.NO_MUTATION_IMPROVEMENT
                    accepted_round = False
                else:
                    reason = None
                    accepted_round = True
            item = MutationFeedbackRoundResult(
                round_index,
                current_evidence,
                prompt,
                raw_response,
                final_candidate,
                execution,
                coverage,
                measured,
                current_mutation,
                next_mutation,
                next_evidence,
                accepted_round,
                newly_killed,
                regressed,
                reason,
                final_attempt.structure,
                final_attempt.quality_warnings,
                generation_duration,
            )
            self.artifact_store.save_mutation_feedback_round_result(artifacts, item)
            rounds.append(item)
            if not accepted_round:
                return self._session_from_current(
                    baseline_mutation,
                    evidence,
                    rounds,
                    accepted,
                    current_mutation,
                    current_evidence,
                    current_coverage,
                    coverage,
                    reason,
                )
            accepted.append(final_candidate.test_file)
            protected = self._snapshot(function.file_path, accepted)
            current_mutation = next_mutation
            current_evidence = next_evidence
            coverage = measured
            if not current_evidence.survivor_ids:
                return self._session_from_current(
                    baseline_mutation,
                    evidence,
                    rounds,
                    accepted,
                    current_mutation,
                    current_evidence,
                    current_coverage,
                    coverage,
                    MutationFeedbackStopReason.ALL_MUTANTS_KILLED,
                )
        return self._session_from_current(
            baseline_mutation,
            evidence,
            rounds,
            accepted,
            current_mutation,
            current_evidence,
            current_coverage,
            coverage,
            MutationFeedbackStopReason.MAX_ROUNDS_REACHED,
        )

    def _record_terminal(
        self,
        artifacts,
        rounds,
        round_index,
        current_evidence,
        prompt,
        raw_response,
        candidate,
        execution,
        coverage_before,
        coverage_after,
        mutation_before,
        mutation_after,
        evidence_after,
        reason,
        structure,
        warnings,
        generation_duration,
        baseline_mutation,
        baseline_evidence,
        accepted,
        initial_coverage,
    ) -> MutationFeedbackSessionResult:
        item = MutationFeedbackRoundResult(
            round_index,
            current_evidence,
            prompt,
            raw_response,
            candidate,
            execution,
            coverage_before,
            coverage_after,
            mutation_before,
            mutation_after,
            evidence_after,
            False,
            (),
            (),
            reason,
            structure,
            warnings,
            generation_duration,
        )
        self.artifact_store.save_mutation_feedback_round_result(artifacts, item)
        rounds.append(item)
        return self._session_from_current(
            baseline_mutation,
            baseline_evidence,
            rounds,
            accepted,
            mutation_before,
            current_evidence,
            initial_coverage,
            coverage_before,
            reason,
        )

    @staticmethod
    def _universe_stable(
        before: MutationResult,
        before_evidence: SurvivorEvidence,
        after: MutationResult,
        after_evidence: SurvivorEvidence,
    ) -> bool:
        return (
            before_evidence.mutant_ids == after_evidence.mutant_ids
            and before.target_file.resolve() == after.target_file.resolve()
            and before.function_name == after.function_name
            and before.backend_version == after.backend_version
            and before.hashes.get("target_source_sha256")
            == after.hashes.get("target_source_sha256")
            and before.hashes.get("mutation_config_sha256")
            == after.hashes.get("mutation_config_sha256")
        )

    @staticmethod
    def _snapshot(source: Path, tests: Sequence[Path]) -> dict[Path, str]:
        return {
            path.resolve(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (source, *tests)
        }

    @staticmethod
    def _verify(expected: Mapping[Path, str]) -> None:
        for path, digest in expected.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise MutationError(f"Mutation feedback changed protected input: {path}")

    @staticmethod
    def _failed_round(round_index, evidence, prompt, mutation, coverage, reason):
        return MutationFeedbackRoundResult(
            round_index,
            evidence,
            prompt,
            None,
            None,
            None,
            coverage,
            None,
            mutation,
            None,
            None,
            False,
            (),
            (),
            reason,
            None,
            (),
            0.0,
        )

    @staticmethod
    def _session(
        baseline,
        final,
        baseline_evidence,
        final_evidence,
        rounds,
        accepted,
        initial_coverage,
        final_coverage,
        reason,
    ) -> MutationFeedbackSessionResult:
        return MutationFeedbackSessionResult(
            baseline,
            final,
            baseline_evidence,
            final_evidence,
            tuple(rounds),
            tuple(accepted),
            initial_coverage,
            final_coverage,
            reason,
        )

    def _session_from_current(
        self,
        baseline,
        baseline_evidence,
        rounds,
        accepted,
        current,
        current_evidence,
        initial_coverage,
        coverage,
        reason,
    ) -> MutationFeedbackSessionResult:
        return self._session(
            baseline,
            current,
            baseline_evidence,
            current_evidence,
            rounds,
            accepted,
            initial_coverage,
            coverage,
            reason,
        )
