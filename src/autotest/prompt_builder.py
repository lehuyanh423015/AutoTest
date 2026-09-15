"""Deterministic prompts for unit-test generation."""

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
