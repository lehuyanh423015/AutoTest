"""Small, deterministic execution-failure summaries for repair prompts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from autotest.test_runner import TestRunResult, TestStatus


class FailureCategory(StrEnum):
    """Coarse failure categories; this is intentionally not a traceback parser."""

    NONE = "NONE"
    SYNTAX_ERROR = "SYNTAX_ERROR"
    IMPORT_ERROR = "IMPORT_ERROR"
    COLLECTION_ERROR = "COLLECTION_ERROR"
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    TIMEOUT = "TIMEOUT"
    OTHER_ERROR = "OTHER_ERROR"


@dataclass(frozen=True, slots=True)
class FailureContext:
    """Raw execution evidence plus a bounded copy suitable for an LLM prompt."""

    status: TestStatus
    category: FailureCategory
    summary: str
    stdout: str
    stderr: str
    feedback: str
    feedback_truncated: bool


class FailureAnalyzer:
    """Classify pytest outcomes and retain the tail of oversized output."""

    def __init__(self, max_feedback_chars: int = 12_000) -> None:
        if max_feedback_chars <= 0:
            raise ValueError("Maximum feedback size must be greater than zero.")
        self.max_feedback_chars = max_feedback_chars

    def analyze(self, result: TestRunResult) -> FailureContext:
        category = self._category(result)
        summary = self._summary(category)
        raw_feedback = f"Pytest stdout:\n{result.stdout}\n\nPytest stderr:\n{result.stderr}"
        feedback, truncated = self._truncate_tail(raw_feedback)
        return FailureContext(
            status=result.status,
            category=category,
            summary=summary,
            stdout=result.stdout,
            stderr=result.stderr,
            feedback=feedback,
            feedback_truncated=truncated,
        )

    def _truncate_tail(self, feedback: str) -> tuple[str, bool]:
        if len(feedback) <= self.max_feedback_chars:
            return feedback, False
        marker = "[Earlier pytest output truncated; tail preserved.]\n"
        if self.max_feedback_chars <= len(marker):
            return feedback[-self.max_feedback_chars :], True
        remaining = max(self.max_feedback_chars - len(marker), 0)
        return f"{marker}{feedback[-remaining:]}", True

    @staticmethod
    def _category(result: TestRunResult) -> FailureCategory:
        if result.status is TestStatus.PASS:
            return FailureCategory.NONE
        if result.status is TestStatus.TIMEOUT:
            return FailureCategory.TIMEOUT
        if result.status is TestStatus.FAIL:
            return FailureCategory.ASSERTION_FAILURE

        evidence = f"{result.stdout}\n{result.stderr}".lower()
        if "syntaxerror" in evidence or "indentationerror" in evidence:
            return FailureCategory.SYNTAX_ERROR
        if any(
            marker in evidence
            for marker in ("importerror", "modulenotfounderror", "no module named")
        ):
            return FailureCategory.IMPORT_ERROR
        if any(marker in evidence for marker in ("error collecting", "collection error")):
            return FailureCategory.COLLECTION_ERROR
        return FailureCategory.OTHER_ERROR

    @staticmethod
    def _summary(category: FailureCategory) -> str:
        return {
            FailureCategory.NONE: "The generated tests passed.",
            FailureCategory.SYNTAX_ERROR: "The generated test contains invalid Python syntax.",
            FailureCategory.IMPORT_ERROR: "The generated test failed while importing code.",
            FailureCategory.COLLECTION_ERROR: "Pytest could not collect the generated tests.",
            FailureCategory.ASSERTION_FAILURE: (
                "Pytest executed the suite and at least one assertion failed."
            ),
            FailureCategory.TIMEOUT: "The generated-test subprocess exceeded its timeout.",
            FailureCategory.OTHER_ERROR: "The generated test encountered another execution error.",
        }[category]
