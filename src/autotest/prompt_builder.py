"""Deterministic prompts for unit-test generation."""

from autotest.coverage_runner import CoverageResult
from autotest.failure_analyzer import FailureContext
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
