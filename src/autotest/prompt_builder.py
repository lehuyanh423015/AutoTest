"""Deterministic prompts for unit-test generation."""

from autotest.coverage_runner import CoverageResult
from autotest.failure_analyzer import FailureContext
from autotest.mutation_runner import SurvivingMutant
from autotest.project_analyzer import FunctionInfo
from autotest.test_runner import TestStatus


class PromptBuilder:
    """Build the constrained Phase 1 pytest-generation prompt."""

    def build(self, function: FunctionInfo) -> str:
        """Return a deterministic prompt for a single function."""
        return f"""You are generating unit tests for one Python function.

Target module: {function.module_name}
Target function: {function.function_name}

Source code:
{function.source_code}

Requirements:
1. Use pytest and generate executable Python test code.
2. Test only the supplied function, including normal behavior and meaningful edge cases.
3. Import the function with: from {function.module_name} import {function.function_name}
4. Do not modify the implementation.
5. Prefer observable behavior over testing implementation details.
6. Use deterministic assertions and pytest.approx when floating-point rounding is relevant.
7. Do not create tests that sleep, hang, or depend on timing.
8. Do not use network access, external services, subprocesses, or install packages.
9. Do not invent functions, classes, modules, or APIs absent from the supplied context.
10. Return only Python source code, without Markdown fences or explanations.
"""

    def build_repair(
        self,
        function: FunctionInfo,
        current_test_code: str,
        failure: FailureContext,
        attempt_number: int,
    ) -> str:
        """Build a deterministic, status-aware repair prompt."""
        status_guidance = self._status_guidance(failure.status)
        truncation_note = (
            "The execution evidence was tail-truncated to the configured character limit."
            if failure.feedback_truncated
            else "The complete captured execution evidence is included."
        )
        return f"""You are repairing a generated pytest test suite.

Repair attempt: {attempt_number}
Target module: {function.module_name}
Target function: {function.function_name}

Target source code:
{function.source_code}

Current generated test code:
{current_test_code}

Execution status: {failure.status.value}
Failure category: {failure.category.value}
Failure summary: {failure.summary}
{truncation_note}

Execution evidence:
{failure.feedback}

Status-specific guidance:
{status_guidance}

Repair constraints:
1. You may modify ONLY the test code. Do not modify the target implementation.
2. Return only complete Python test source code, without Markdown fences or explanations.
3. Do not install packages, use network access, or depend on external services.
4. Do not use sleep unless absolutely necessary or create blocking or long-running tests.
5. Preserve useful existing tests and fix only what execution evidence justifies.
6. Do not delete or weaken meaningful assertions merely to obtain a passing result.
7. Do not replace assertions with tautologies such as assert True or result == result.
8. Do not assert a value derived from the same invocation being tested.
9. Do not copy observed output into an assertion unless target source logically justifies it.
"""

    def build_coverage(
        self,
        function: FunctionInfo,
        coverage: CoverageResult,
        accepted_test_sources: tuple[str, ...],
        round_number: int,
        *,
        max_accepted_test_chars: int = 12_000,
    ) -> str:
        """Build deterministic feedback for one supplementary coverage test module."""
        if round_number < 1:
            raise ValueError("Coverage round number must be greater than or equal to one.")
        if max_accepted_test_chars < 1:
            raise ValueError("Accepted-test context limit must be greater than zero.")
        numbered_source = "\n".join(
            f"{line_number} | {line}"
            for line_number, line in enumerate(
                function.source_code.splitlines(), start=function.start_line
            )
        )
        missing_lines = ", ".join(str(line) for line in coverage.missing_lines) or "None"
        missing_branches = (
            "\n".join(
                f"{origin} -> {destination}" for origin, destination in coverage.missing_branches
            )
            or "None (branch coverage is not applicable or no branches are missing)"
        )
        accepted_context = "\n\n".join(
            f"Accepted test module {index}:\n{source.rstrip()}"
            for index, source in enumerate(accepted_test_sources, start=1)
        )
        if len(accepted_context) > max_accepted_test_chars:
            marker = "\n[accepted-test context truncated at configured character limit]"
            accepted_context = accepted_context[: max_accepted_test_chars - len(marker)] + marker
        branch_percent = (
            "N/A"
            if coverage.branch_coverage_percent is None
            else f"{coverage.branch_coverage_percent:.2f}%"
        )
        return f"""You are generating ADDITIONAL pytest tests for an existing passing test suite.

Coverage round: {round_number}
Target module: {function.module_name}
Target function: {function.function_name}

Target function source with actual source-file line numbers:
{numbered_source}

Current target-function coverage:
Line coverage: {coverage.line_coverage_percent:.2f}%
Branch coverage: {branch_percent}
Missing executable lines: {missing_lines}
Missing branch arcs:
{missing_branches}

Existing accepted tests (bounded context):
{accepted_context or "None"}

Requirements:
1. Generate only new tests that focus specifically on the listed missing lines and branch arcs.
2. Do not rewrite, reproduce, remove, weaken, or modify existing accepted tests.
3. Import the target with: from {function.module_name} import {function.function_name}
4. Use deterministic assertions whose expected values are logically supported by the source.
5. Do not use observed runtime output as the oracle.
6. Do not create tautological assertions, use assert True, or assert a value against itself.
7. Do not use network access, external services, subprocesses, or install packages.
8. Do not modify the target implementation.
9. Do not intentionally sleep or create blocking or long-running tests.
10. Return only complete Python source code for one supplementary pytest module.
11. Do not include Markdown fences or explanations.
"""

    def build_mutation_feedback(
        self,
        function: FunctionInfo,
        accepted_test_sources: tuple[str, ...],
        survivors: tuple[SurvivingMutant, ...],
        round_number: int,
        *,
        max_chars: int = 12_000,
    ) -> str:
        """Build bounded, deterministic original-behavior mutation feedback."""
        if round_number < 1:
            raise ValueError("Mutation round number must be greater than or equal to one.")
        if max_chars < 4_000:
            raise ValueError("Mutation feedback limit must be at least 4000 characters.")
        if not survivors:
            raise ValueError("Mutation feedback requires at least one surviving mutant.")
        numbered_source = "\n".join(
            f"{line_number} | {line}"
            for line_number, line in enumerate(
                function.source_code.splitlines(), start=function.start_line
            )
        )
        accepted = (
            "\n\n".join(
                f"Accepted test module {index}:\n{source.rstrip()}"
                for index, source in enumerate(accepted_test_sources, start=1)
            )
            or "None"
        )
        evidence = "\n\n".join(
            f"Surviving mutant: {item.mutant_id}\nExact Mutmut diff:\n{item.diff.rstrip()}"
            for item in survivors
        )
        requirements = "\n".join(
            (
                "Requirements:",
                "1. Generate only ADDITIONAL pytest tests that distinguish the ORIGINAL "
                "implementation from defensible behavioral changes shown in the surviving "
                "mutation diffs.",
                f"2. Import the target with: from {function.module_name} import "
                f"{function.function_name}",
                "3. The supplied implementation is ORIGINAL; diffs are artificial changes. "
                "Justify expected values from the original source, never mutant behavior or "
                "observed mutant output.",
                "4. Do not rewrite, reproduce, remove, weaken, or modify existing accepted tests.",
                "5. Do not modify the original implementation, apply mutants, or alter "
                "mutation operators.",
                "6. Do not create tautologies, use assert True, compare a result to itself, "
                "or derive an oracle from the same invocation.",
                "7. Do not use network access, external services, subprocesses, package "
                "installation, or intentional sleep.",
                "8. Some surviving mutants may be behaviorally equivalent or not "
                "distinguishable by a meaningful test. Do not invent arbitrary assertions "
                "merely to force every supplied mutant to be killed. Generate a test only "
                "when the original source provides a defensible expected behavior.",
                "9. Return only complete Python source code for one ADDITIONAL pytest module, "
                "without Markdown fences or explanations.",
                "",
            )
        )
        fixed = f"""You are generating ADDITIONAL pytest tests for an existing passing suite.

Mutation feedback round: {round_number}
Original target module: {function.module_name}
Original target function: {function.function_name}

ORIGINAL target function source with actual source-file line numbers:
{{source}}

Existing accepted tests (bounded context):
{{accepted}}

Selected surviving-mutant evidence (bounded context):
{{evidence}}

{requirements}"""
        empty_size = len(fixed.format(source="", accepted="", evidence=""))
        budget = max_chars - empty_size
        if budget < 3:
            raise ValueError("Mutation feedback limit is too small for required constraints.")
        source_budget = max(1, budget // 3)
        accepted_budget = max(1, budget // 3)
        evidence_budget = max(1, budget - source_budget - accepted_budget)
        prompt = fixed.format(
            source=self._bounded(numbered_source, source_budget),
            accepted=self._bounded(accepted, accepted_budget),
            evidence=self._bounded(evidence, evidence_budget),
        )
        return prompt[:max_chars]

    @staticmethod
    def _bounded(value: str, limit: int) -> str:
        marker = "\n[context truncated at configured character limit]"
        if len(value) <= limit:
            return value
        if limit <= len(marker):
            return marker[:limit]
        return value[: limit - len(marker)] + marker

    @staticmethod
    def _status_guidance(status: TestStatus) -> str:
        if status is TestStatus.ERROR:
            return (
                "Fix syntax, invalid imports, invented APIs, invalid fixtures, or pytest "
                "collection structure. Use only the supplied module and target function."
            )
        if status is TestStatus.TIMEOUT:
            return (
                "Remove unnecessary waiting, infinite loops, blocking network or external-resource "
                "behavior, and excessive iteration. Keep tests small and deterministic. Do not "
                "increase the timeout to hide the problem."
            )
        if status is TestStatus.FAIL:
            return (
                "Decide conservatively whether the expectation is unsupported or the target may "
                "be defective. Change an expected value only when the supplied source clearly "
                "justifies it. If the assertion could reveal a source defect, preserve it. Do not "
                "remove failing edge cases, weaken assertions, or fabricate a passing expectation."
            )
        return "No repair is required for a passing test suite."
