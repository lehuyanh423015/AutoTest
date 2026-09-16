import json
from pathlib import Path
from unittest.mock import patch

import pytest

from autotest.coverage_engine import CoverageSessionResult, CoverageStopReason
from autotest.coverage_runner import CoverageResult
from autotest.errors import AnalyzerError
from autotest.failure_analyzer import FailureAnalyzer
from autotest.main import main
from autotest.mutation_feedback import (
    MutationFeedbackSessionResult,
    MutationFeedbackStopReason,
)
from autotest.mutation_runner import MutationResult, MutationStatus
from autotest.repair_engine import (
    AttemptKind,
    RepairSessionResult,
    StopReason,
)
from autotest.repair_engine import TestAttempt as Attempt
from autotest.test_quality import analyze_test_structure
from autotest.test_runner import TestRunResult as RunResult
from autotest.test_runner import TestStatus as RunStatus


def make_session(tmp_path: Path, statuses: list[RunStatus]) -> RepairSessionResult:
    attempts: list[Attempt] = []
    for index, status in enumerate(statuses):
        test_file = tmp_path / f"attempt-{index:03d}" / "generated_test.py"
        test_file.parent.mkdir(exist_ok=True)
        test_code = "def test_add():\n    assert add(2, 3) == 5\n"
        exit_code = {
            RunStatus.PASS: 0,
            RunStatus.FAIL: 1,
            RunStatus.ERROR: 2,
            RunStatus.TIMEOUT: None,
        }[status]
        result = RunResult(test_file, exit_code, status, "pytest output", "", 0.1)
        attempts.append(
            Attempt(
                attempt_index=index,
                kind=AttemptKind.INITIAL if index == 0 else AttemptKind.REPAIR,
                test_code=test_code,
                test_file=test_file,
                prompt=f"prompt {index}",
                raw_response=f"response {index}",
                run_result=result,
                failure_context=FailureAnalyzer().analyze(result),
                structure=analyze_test_structure(test_code),
                quality_warnings=(),
                generation_duration_seconds=0.2,
            )
        )
    return RepairSessionResult(
        attempts=tuple(attempts),
        initial_status=statuses[0],
        final_status=statuses[-1],
        repair_count=len(statuses) - 1,
        repaired_successfully=(
            statuses[0] is not RunStatus.PASS and statuses[-1] is RunStatus.PASS
        ),
        stopped_reason=(
            StopReason.PASS_REACHED
            if statuses[-1] is RunStatus.PASS
            else StopReason.MAX_REPAIRS_REACHED
        ),
        total_generation_seconds=0.2 * len(statuses),
        total_execution_seconds=0.1 * len(statuses),
    )


def run_mocked_cli(
    tmp_path: Path,
    session: RepairSessionResult,
    *extra_args: str,
    mutation_result: MutationResult | None = None,
    mutation_feedback_result: MutationFeedbackSessionResult | None = None,
) -> tuple[int, object]:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    coverage_result = CoverageResult(
        source.resolve(), "add", (1, 2), (1, 2), (), (), (), 100.0, None, None, 0.1
    )
    coverage_session = CoverageSessionResult(
        coverage_result,
        coverage_result,
        (),
        (session.attempts[-1].test_file,),
        (coverage_result,),
        True,
        CoverageStopReason.TARGET_REACHED,
    )
    with (
        patch("autotest.main.RepairEngine") as engine_class,
        patch("autotest.main.CoverageEngine") as coverage_engine_class,
        patch("autotest.main.WSLMutmutBackend") as mutation_backend_class,
        patch("autotest.main.MutationFeedbackEngine") as mutation_feedback_engine_class,
    ):
        engine_class.return_value.run.return_value = session
        coverage_engine_class.return_value.run.return_value = coverage_session
        mutation_backend_class.return_value.run.return_value = mutation_result
        mutation_feedback_engine_class.return_value.run.return_value = mutation_feedback_result
        exit_code = main(
            [
                "--file",
                str(source),
                "--function",
                "add",
                "--output-dir",
                str(tmp_path / "runs"),
                *extra_args,
            ]
        )
    return exit_code, engine_class


def test_cli_initial_pass_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, _ = run_mocked_cli(tmp_path, make_session(tmp_path, [RunStatus.PASS]))

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Initial status: PASS" in output
    assert "Final status: PASS" in output
    assert "Repairs used: 0" in output
    assert "Exit code: 0" in output
    assert "Run artifact directory:" in output
    run_dir = next((tmp_path / "runs").iterdir())
    assert (run_dir / "result.json").is_file()


def test_cli_repair_success_uses_final_pass_exit_code(tmp_path: Path) -> None:
    session = make_session(tmp_path, [RunStatus.ERROR, RunStatus.PASS])

    exit_code, _ = run_mocked_cli(tmp_path, session)

    assert exit_code == 0


@pytest.mark.parametrize(
    ("status", "expected_cli_exit_code"),
    [
        (RunStatus.FAIL, 1),
        (RunStatus.ERROR, 2),
        (RunStatus.TIMEOUT, 3),
    ],
)
def test_cli_exhausted_repair_exit_code_contract(
    tmp_path: Path,
    status: RunStatus,
    expected_cli_exit_code: int,
) -> None:
    exit_code, _ = run_mocked_cli(tmp_path, make_session(tmp_path, [status]))

    assert exit_code == expected_cli_exit_code


def test_cli_zero_repairs_is_passed_to_engine(tmp_path: Path) -> None:
    exit_code, engine_class = run_mocked_cli(
        tmp_path,
        make_session(tmp_path, [RunStatus.ERROR]),
        "--max-repair-attempts",
        "0",
    )

    assert exit_code == 2
    assert engine_class.call_args.kwargs["max_repair_attempts"] == 0  # type: ignore[attr-defined]


def test_cli_coverage_options_are_validated_and_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, _ = run_mocked_cli(
        tmp_path,
        make_session(tmp_path, [RunStatus.PASS]),
        "--max-coverage-rounds",
        "0",
        "--coverage-target",
        "75.5",
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Coverage baseline: line 100.00%, branch N/A" in output
    assert "Coverage target reached: YES" in output
    result = next((tmp_path / "runs").iterdir()) / "result.json"
    assert '"target": 75.5' in result.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--max-coverage-rounds", "-1"),
        ("--coverage-target", "-0.1"),
        ("--coverage-target", "100.1"),
        ("--coverage-target", "nan"),
    ],
)
def test_cli_rejects_invalid_coverage_options(tmp_path: Path, option: str, value: str) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["--file", str(source), "--function", "add", option, value])

    assert exc_info.value.code == 2


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_cli_rejects_invalid_mutation_timeout(tmp_path: Path, value: str) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["--file", str(source), "--function", "add", "--mutation-timeout", value])

    assert exc_info.value.code == 2


def test_cli_mutation_success_preserves_pass_exit_and_root_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "calculator.py"
    result = MutationResult(
        source,
        "add",
        MutationStatus.COMPLETE,
        total_mutants=4,
        killed_mutants=1,
        survived_mutants=3,
        tool_total_mutants=4,
        mutation_score_percent=25.0,
        duration_seconds=2.0,
        backend_version="3.7.0",
    )

    exit_code, _ = run_mocked_cli(
        tmp_path,
        make_session(tmp_path, [RunStatus.PASS]),
        "--mutation",
        mutation_result=result,
    )

    assert exit_code == 0
    assert "Mutation score: 25.00%" in capsys.readouterr().out
    root = json.loads((next((tmp_path / "runs").iterdir()) / "result.json").read_text())
    assert root["mutation"]["status"] == "COMPLETE"
    assert root["mutation"]["final_line_coverage"] == 100.0


@pytest.mark.parametrize(
    "status",
    [MutationStatus.TOOL_UNAVAILABLE, MutationStatus.TOOL_ERROR, MutationStatus.TIMEOUT],
)
def test_cli_requested_mutation_infrastructure_error_uses_exit_two(
    tmp_path: Path, status: MutationStatus
) -> None:
    result = MutationResult(tmp_path / "calculator.py", "add", status, error_message="tool error")

    exit_code, _ = run_mocked_cli(
        tmp_path,
        make_session(tmp_path, [RunStatus.PASS]),
        "--mutation",
        mutation_result=result,
    )

    assert exit_code == 2


def test_cli_does_not_run_requested_mutation_when_tests_fail(tmp_path: Path) -> None:
    exit_code, _ = run_mocked_cli(tmp_path, make_session(tmp_path, [RunStatus.FAIL]), "--mutation")

    assert exit_code == 1
    root = json.loads((next((tmp_path / "runs").iterdir()) / "result.json").read_text())
    assert root["mutation"]["skipped_reason"] == "EXECUTION_NOT_PASSING"


def test_cli_mutation_feedback_implies_mutation_and_persists_separate_metrics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    session = make_session(tmp_path, [RunStatus.PASS])
    source = tmp_path / "calculator.py"
    baseline = MutationResult(
        source,
        "add",
        MutationStatus.COMPLETE,
        total_mutants=2,
        killed_mutants=1,
        survived_mutants=1,
        tool_total_mutants=2,
        mutation_score_percent=50.0,
        backend_version="3.7.0",
    )
    coverage = CoverageResult(
        source.resolve(), "add", (1, 2), (1, 2), (), (), (), 100.0, None, None, 0.1
    )
    feedback = MutationFeedbackSessionResult(
        baseline,
        baseline,
        None,
        None,
        (),
        (session.attempts[-1].test_file,),
        coverage,
        coverage,
        MutationFeedbackStopReason.MAX_ROUNDS_REACHED,
    )

    exit_code, _ = run_mocked_cli(
        tmp_path,
        session,
        "--mutation-feedback",
        "--max-mutation-rounds",
        "0",
        mutation_result=baseline,
        mutation_feedback_result=feedback,
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Mutation feedback:" in output
    root = json.loads((next((tmp_path / "runs").iterdir()) / "result.json").read_text())
    assert root["mutation"]["enabled"] is True
    assert root["mutation"]["status"] == "COMPLETE"
    assert root["mutation_feedback"]["enabled"] is True
    assert root["mutation_feedback"]["max_rounds"] == 0
    assert root["mutation_feedback"]["metrics"]["mutation_feedback_llm_calls"] == 0


@pytest.mark.parametrize(
    ("option", "value"),
    [("--max-mutation-rounds", "-1"), ("--max-mutants-per-round", "0")],
)
def test_cli_rejects_invalid_mutation_feedback_bounds(
    tmp_path: Path, option: str, value: str
) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(["--file", str(source), "--function", "add", option, value])

    assert exc_info.value.code == 2


def test_cli_rejects_negative_repair_count(tmp_path: Path) -> None:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--file",
                str(source),
                "--function",
                "add",
                "--max-repair-attempts",
                "-1",
            ]
        )

    assert exc_info.value.code == 2


def test_cli_expected_error_has_no_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "missing.py"

    with patch(
        "autotest.main.ProjectAnalyzer.analyze_function",
        side_effect=AnalyzerError("friendly failure"),
    ):
        exit_code = main(["--file", str(missing), "--function", "add"])

    assert exit_code == 2
    assert "friendly failure" in caplog.text
    assert "Traceback" not in caplog.text
