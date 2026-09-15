from pathlib import Path

import pytest

from autotest.failure_analyzer import FailureAnalyzer, FailureCategory
from autotest.test_runner import TestRunResult as RunResult
from autotest.test_runner import TestStatus as RunStatus


def result(status: RunStatus, stdout: str = "", stderr: str = "") -> RunResult:
    exit_code = {
        RunStatus.PASS: 0,
        RunStatus.FAIL: 1,
        RunStatus.ERROR: 2,
        RunStatus.TIMEOUT: None,
    }[status]
    return RunResult(Path("generated_test.py"), exit_code, status, stdout, stderr, 0.1)


@pytest.mark.parametrize(
    ("run_result", "category"),
    [
        (result(RunStatus.ERROR, "SyntaxError: invalid syntax"), FailureCategory.SYNTAX_ERROR),
        (
            result(RunStatus.ERROR, stderr="ModuleNotFoundError: No module named 'missing'"),
            FailureCategory.IMPORT_ERROR,
        ),
        (result(RunStatus.FAIL, "assert 4 == 5"), FailureCategory.ASSERTION_FAILURE),
        (
            result(RunStatus.ERROR, "ERROR collecting generated_test.py"),
            FailureCategory.COLLECTION_ERROR,
        ),
        (result(RunStatus.TIMEOUT), FailureCategory.TIMEOUT),
        (result(RunStatus.ERROR, "RuntimeError: unexpected"), FailureCategory.OTHER_ERROR),
    ],
)
def test_failure_classification(run_result: RunResult, category: FailureCategory) -> None:
    context = FailureAnalyzer().analyze(run_result)

    assert context.category is category
    assert context.stdout == run_result.stdout
    assert context.stderr == run_result.stderr
    assert context.summary


def test_large_feedback_preserves_tail_and_raw_output() -> None:
    stdout = "beginning-" + ("x" * 200) + "-important traceback tail"

    context = FailureAnalyzer(max_feedback_chars=120).analyze(
        result(RunStatus.ERROR, stdout=stdout)
    )

    assert context.feedback_truncated is True
    assert len(context.feedback) == 120
    assert context.feedback.endswith("important traceback tail\n\nPytest stderr:\n")
    assert context.stdout == stdout


def test_tiny_feedback_limit_is_always_respected() -> None:
    context = FailureAnalyzer(max_feedback_chars=8).analyze(
        result(RunStatus.ERROR, stdout="a" * 100)
    )

    assert len(context.feedback) == 8
