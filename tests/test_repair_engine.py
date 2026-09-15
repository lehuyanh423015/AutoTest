from collections.abc import Sequence
from pathlib import Path

import pytest

from autotest.artifact_store import ArtifactStore
from autotest.errors import LLMConnectionError
from autotest.llm.base import LLMProvider
from autotest.project_analyzer import FunctionInfo
from autotest.repair_engine import RepairEngine, RepairSessionResult, StopReason
from autotest.test_generator import GeneratedTest
from autotest.test_runner import TestRunner as Runner
from autotest.test_runner import TestRunResult as RunResult
from autotest.test_runner import TestStatus as RunStatus


class ScriptedProvider(LLMProvider):
    def __init__(self, responses: Sequence[str | Exception]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ScriptedRunner:
    def __init__(self, statuses: Sequence[RunStatus]) -> None:
        self.statuses = list(statuses)
        self.calls: list[Path] = []
        self.timeout = 30.0

    def run(self, test_file: Path | str, project_root: Path | str) -> RunResult:
        path = Path(test_file)
        self.calls.append(path)
        status = self.statuses.pop(0)
        stdout = {
            RunStatus.PASS: "1 passed",
            RunStatus.FAIL: "AssertionError: assert 6 == 5",
            RunStatus.ERROR: "SyntaxError: invalid syntax",
            RunStatus.TIMEOUT: "partial output",
        }[status]
        exit_code = {
            RunStatus.PASS: 0,
            RunStatus.FAIL: 1,
            RunStatus.ERROR: 2,
            RunStatus.TIMEOUT: None,
        }[status]
        return RunResult(path, exit_code, status, stdout, "", 0.1)


def make_function(tmp_path: Path) -> FunctionInfo:
    source = tmp_path / "calculator.py"
    source.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return FunctionInfo(
        file_path=source.resolve(),
        module_name="calculator",
        function_name="add",
        source_code="def add(a, b):\n    return a + b",
        start_line=1,
        end_line=2,
        docstring=None,
    )


def generated_code(index: int) -> str:
    return f"def test_add_{index}():\n    assert add(2, 3) == 5\n"


def execute_session(
    tmp_path: Path,
    statuses: Sequence[RunStatus],
    max_repairs: int,
) -> tuple[RepairSessionResult, ScriptedProvider, ScriptedRunner, Path]:
    provider = ScriptedProvider([generated_code(index) for index in range(len(statuses))])
    runner = ScriptedRunner(statuses)
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    session = RepairEngine(
        provider,
        runner,
        store,
        max_repair_attempts=max_repairs,
    ).run(make_function(tmp_path), run)
    return session, provider, runner, run.run_dir


@pytest.mark.parametrize(
    ("statuses", "expected_repairs"),
    [
        ([RunStatus.ERROR, RunStatus.PASS], 1),
        ([RunStatus.ERROR, RunStatus.ERROR, RunStatus.PASS], 2),
        ([RunStatus.FAIL, RunStatus.PASS], 1),
        ([RunStatus.TIMEOUT, RunStatus.PASS], 1),
    ],
)
def test_nonpassing_results_can_repair_to_pass(
    tmp_path: Path,
    statuses: list[RunStatus],
    expected_repairs: int,
) -> None:
    session, provider, runner, run_dir = execute_session(tmp_path, statuses, max_repairs=3)

    assert session.initial_status is statuses[0]
    assert session.final_status is RunStatus.PASS
    assert session.repair_count == expected_repairs
    assert session.repaired_successfully is True
    assert session.stopped_reason is StopReason.PASS_REACHED
    assert len(session.attempts) == len(statuses)
    assert len(provider.prompts) == len(statuses)
    assert len(runner.calls) == len(statuses)
    for index in range(len(statuses)):
        attempt_dir = run_dir / f"attempt-{index:03d}"
        assert (attempt_dir / "prompt.txt").is_file()
        assert (attempt_dir / "raw_response.txt").is_file()
        assert (attempt_dir / "generated_test.py").is_file()
        assert (attempt_dir / "result.json").is_file()


def test_repair_loop_stops_at_configured_maximum(tmp_path: Path) -> None:
    statuses = [RunStatus.ERROR] * 4

    session, provider, runner, _ = execute_session(tmp_path, statuses, max_repairs=3)

    assert len(session.attempts) == 4
    assert session.repair_count == 3
    assert session.final_status is RunStatus.ERROR
    assert session.stopped_reason is StopReason.MAX_REPAIRS_REACHED
    assert session.repaired_successfully is False
    assert len(provider.prompts) == 4
    assert len(runner.calls) == 4


def test_pass_does_not_trigger_repair(tmp_path: Path) -> None:
    session, provider, runner, _ = execute_session(tmp_path, [RunStatus.PASS], max_repairs=3)

    assert len(session.attempts) == 1
    assert session.repair_count == 0
    assert session.repaired_successfully is False
    assert session.stopped_reason is StopReason.PASS_REACHED
    assert len(provider.prompts) == 1
    assert len(runner.calls) == 1


def test_zero_max_repairs_reproduces_single_execution(tmp_path: Path) -> None:
    session, provider, runner, _ = execute_session(tmp_path, [RunStatus.ERROR], max_repairs=0)

    assert len(session.attempts) == 1
    assert session.repair_count == 0
    assert session.final_status is RunStatus.ERROR
    assert session.stopped_reason is StopReason.MAX_REPAIRS_REACHED
    assert len(provider.prompts) == 1
    assert len(runner.calls) == 1


def test_provider_failure_stops_safely_and_preserves_repair_prompt(tmp_path: Path) -> None:
    provider = ScriptedProvider(
        [generated_code(0), LLMConnectionError("provider unavailable during repair")]
    )
    runner = ScriptedRunner([RunStatus.ERROR])
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()

    session = RepairEngine(provider, runner, store).run(make_function(tmp_path), run)

    assert session.final_status is RunStatus.ERROR
    assert session.stopped_reason is StopReason.LLM_ERROR
    assert session.repair_count == 0
    assert len(session.attempts) == 1
    assert len(provider.prompts) == 2
    assert (run.run_dir / "attempt-001" / "prompt.txt").is_file()
    assert not (run.run_dir / "attempt-001" / "raw_response.txt").exists()


def test_negative_max_repairs_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="greater than or equal to zero"):
        RepairEngine(
            ScriptedProvider([generated_code(0)]),
            ScriptedRunner([RunStatus.PASS]),
            ArtifactStore(tmp_path),
            max_repair_attempts=-1,
        )


def test_invalid_repair_python_executes_as_error_without_crashing(tmp_path: Path) -> None:
    function = make_function(tmp_path)
    provider = ScriptedProvider(["def still_broken(:\n"])
    store = ArtifactStore(tmp_path / "runs")
    run = store.create_run()
    initial = GeneratedTest(
        "add",
        tmp_path / "unused.py",
        "intentional broken initial test",
        "def broken(:\n",
        "def broken(:\n",
    )

    session = RepairEngine(
        provider,
        Runner(timeout=10),
        store,
        max_repair_attempts=1,
    ).run(function, run, initial_test=initial)

    assert [attempt.run_result.status for attempt in session.attempts] == [
        RunStatus.ERROR,
        RunStatus.ERROR,
    ]
    assert session.stopped_reason is StopReason.MAX_REPAIRS_REACHED
    assert "SyntaxError" in session.attempts[-1].run_result.stdout
