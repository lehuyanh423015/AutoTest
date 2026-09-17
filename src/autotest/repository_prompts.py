"""Repository-aware variants of the frozen prompt contracts."""

from __future__ import annotations

from collections.abc import Callable

from autotest.context_selector import ContextBundle
from autotest.coverage_runner import CoverageResult
from autotest.failure_analyzer import FailureContext
from autotest.mutation_runner import SurvivingMutant
from autotest.project_analyzer import FunctionInfo
from autotest.prompt_builder import PromptBuilder


class RepositoryPromptBuilder(PromptBuilder):
    """Add exact project-relative evidence without altering standalone prompts."""

    def __init__(self, bundle: ContextBundle, module_name: str, verify: Callable[[], None]):
        self.bundle = bundle
        self.module_name = module_name
        self.verify = verify

    def _repository_context(self) -> str:
        self.verify()
        return (
            "\nRepository target (project-relative): "
            f"{self.bundle.target.file.as_posix()}:{self.bundle.target.function}\n"
            f"Target import: from {self.module_name} import {self.bundle.target.function}\n"
            "The following is static implementation evidence, not an external specification. "
            "Generate tests only for the selected target, not separately for its helpers.\n"
            f"{self.bundle.render()}\n"
            "Repository safety constraints: do not modify source files, existing accepted "
            "tests, environment files, or package installations. Do not use network access, "
            "package managers, subprocesses, original checkout paths, or timing-dependent tests.\n"
        )

    def build(self, function: FunctionInfo) -> str:
        return super().build(function) + self._repository_context()

    def build_repair(
        self,
        function: FunctionInfo,
        current_test_code: str,
        failure: FailureContext,
        attempt_number: int,
    ) -> str:
        return (
            super().build_repair(function, current_test_code, failure, attempt_number)
            + self._repository_context()
        )

    def build_coverage(
        self,
        function: FunctionInfo,
        coverage: CoverageResult,
        accepted_test_sources: tuple[str, ...],
        round_number: int,
        *,
        max_accepted_test_chars: int = 12_000,
    ) -> str:
        return (
            super().build_coverage(
                function,
                coverage,
                accepted_test_sources,
                round_number,
                max_accepted_test_chars=max_accepted_test_chars,
            )
            + self._repository_context()
        )

    def build_mutation_feedback(
        self,
        function: FunctionInfo,
        accepted_test_sources: tuple[str, ...],
        survivors: tuple[SurvivingMutant, ...],
        round_number: int,
        *,
        max_chars: int = 12_000,
    ) -> str:
        # The frozen mutation prompt remains bounded; append repository evidence
        # only when a repository-capable backend is explicitly supplied.
        return (
            super().build_mutation_feedback(
                function,
                accepted_test_sources,
                survivors,
                round_number,
                max_chars=max_chars,
            )
            + self._repository_context()
        )
