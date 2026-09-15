from pathlib import Path

from autotest.failure_analyzer import FailureAnalyzer, FailureContext
from autotest.project_analyzer import FunctionInfo
from autotest.prompt_builder import PromptBuilder
from autotest.test_runner import TestRunResult as RunResult
from autotest.test_runner import TestStatus as RunStatus


def function_info() -> FunctionInfo:
    return FunctionInfo(
        file_path=Path("calculator.py"),
        module_name="calculator",
        function_name="divide",
        source_code="def divide(a, b):\n    return a / b",
        start_line=1,
        end_line=2,
        docstring=None,
    )


def test_prompt_contains_target_source_and_constraints() -> None:
    prompt = PromptBuilder().build(function_info())

    assert "Target function: divide" in prompt
    assert "def divide(a, b):" in prompt
    assert "Use pytest" in prompt
    assert "pytest.approx" in prompt
    assert "from calculator import divide" in prompt
    assert "without Markdown fences" in prompt


def test_prompt_is_deterministic() -> None:
    builder = PromptBuilder()
    info = function_info()

    assert builder.build(info) == builder.build(info)


def failure_context(status: RunStatus) -> FailureContext:
    exit_code = {
        RunStatus.PASS: 0,
        RunStatus.FAIL: 1,
        RunStatus.ERROR: 2,
        RunStatus.TIMEOUT: None,
    }[status]
    result = RunResult(
        Path("generated_test.py"),
        exit_code,
        status,
        "assert 6 == 5",
        "failure details",
        0.1,
    )
    return FailureAnalyzer().analyze(result)


def test_repair_prompt_contains_context_evidence_and_constraints() -> None:
    context = failure_context(RunStatus.ERROR)

    prompt = PromptBuilder().build_repair(
        function_info(),
        "def test_divide():\n    assert divide(4, 2) == 3",
        context,
        attempt_number=1,
    )

    assert "def divide(a, b):" in prompt
    assert "def test_divide():" in prompt
    assert "Execution status: ERROR" in prompt
    assert "assert 6 == 5" in prompt
    assert "failure details" in prompt
    assert "modify ONLY the test code" in prompt
    assert "Do not replace assertions with tautologies" in prompt
    assert "Fix syntax, invalid imports" in prompt


def test_fail_repair_prompt_has_conservative_oracle_guidance_and_is_deterministic() -> None:
    builder = PromptBuilder()
    context = failure_context(RunStatus.FAIL)
    args = (
        function_info(),
        "def test_divide():\n    assert divide(4, 2) == 3",
        context,
        2,
    )

    first = builder.build_repair(*args)
    second = builder.build_repair(*args)

    assert first == second
    assert "target may be defective" in first
    assert "preserve it" in first
    assert "Do not remove failing edge cases" in first


def test_timeout_repair_prompt_does_not_hide_problem_by_increasing_timeout() -> None:
    prompt = PromptBuilder().build_repair(
        function_info(),
        "def test_slow():\n    time.sleep(60)",
        failure_context(RunStatus.TIMEOUT),
        1,
    )

    assert "Remove unnecessary waiting" in prompt
    assert "Do not increase the timeout" in prompt
